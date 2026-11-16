#!/bin/bash
# Build the self-hosted basemap tileset with Planetiler.
#
# Layout note: the tileset volume (/mnt/sdb1) is a 7200rpm HDD, and Planetiler's
# temp directory is random-I/O heavy enough that building there is many times slower
# than building on the NVMe. So scratch lives on the NVMe and only the finished
# archive lands on the tileset volume. Source .pbf files stay on the HDD -- they are
# read sequentially, which spinning disks handle fine.
#
# Usage: build.sh planet | cfd | russia | merge [high-zoom.mbtiles]
set -euo pipefail

TILESETS=${TILESETS:-/mnt/sdb1/tilesets}
TMPDIR_HOST=${TMPDIR_HOST:-/var/tmp/planetiler-tmp}
HEAP=${HEAP:-8g}
IMAGE=ghcr.io/onthegomap/planetiler:latest

mkdir -p "$TMPDIR_HOST" "$TILESETS/out"

run_planetiler() {
  docker run --rm \
    -e JAVA_TOOL_OPTIONS="-Xmx$HEAP" \
    -v "$TILESETS":/data \
    -v "$TMPDIR_HOST":/scratch \
    "$IMAGE" "$@" --tmpdir=/scratch --download --force
}

case "${1:-}" in
  planet)
    # z0-12 worldwide: real OSM context everywhere the discovered nodes appear.
    run_planetiler \
      --osm-path=/data/src/planet-latest.osm.pbf \
      --output=/data/out/planet-z12.mbtiles \
      --minzoom=0 --maxzoom=12 \
      --nodemap-type=array --storage=mmap
    ;;
  russia)
    # z13-14 over all of Russia, superseding the Central Federal District build.
    #
    # sparsearray, not array: array indexes the node map by OSM node id, so on a country
    # extract it still allocates for the planet's highest id -- 113GB of scratch to hold
    # a fraction of that. sparsearray sizes to the nodes actually present.
    run_planetiler \
      --osm-path=/data/src/russia-latest.osm.pbf \
      --output=/data/out/russia-z1314.mbtiles \
      --minzoom=13 --maxzoom=14 \
      --nodemap-type=sparsearray --storage=mmap
    ;;
  cfd)
    # z13-14 over the Central Federal District only. The zoom ranges are deliberately
    # disjoint from the planet build: overlapping ranges would make tile-join merge two
    # copies of the same features into one tile.
    run_planetiler \
      --osm-path=/data/src/central-fed-district-latest.osm.pbf \
      --output=/data/out/cfd-z1314.mbtiles \
      --minzoom=13 --maxzoom=14
    ;;
  merge)
    command -v pmtiles >/dev/null || { echo "pmtiles CLI not installed"; exit 1; }
    mkdir -p "$TILESETS/static/tiles"
    # Merge on the NVMe: this rewrites every tile, which is exactly the random I/O the
    # HDD is bad at. Only the finished archive is written to the tileset volume.
    # Second argument names the high-zoom half, so the same merge serves the CFD and
    # the all-Russia builds without editing the script.
    HIZOOM=${2:-$TILESETS/out/russia-z1314.mbtiles}
    [ -f "$HIZOOM" ] || { echo "high-zoom archive not found: $HIZOOM"; exit 1; }
    echo "merging $TILESETS/out/planet-z12.mbtiles + $HIZOOM"
    python3 "$(dirname "$0")/merge_mbtiles.py" \
      "$TMPDIR_HOST/basemap.mbtiles" \
      "$TILESETS/out/planet-z12.mbtiles" \
      "$HIZOOM"
    # Convert to a temp name and rename into place. Writing basemap.pmtiles directly
    # would have the live map serving a half-written archive for the whole conversion,
    # and would truncate whatever that path is currently hardlinked to. rename(2) is
    # atomic within the filesystem, and readers holding the old file keep serving it.
    pmtiles convert "$TMPDIR_HOST/basemap.mbtiles" "$TILESETS/static/tiles/basemap.pmtiles.new"
    mv -f "$TILESETS/static/tiles/basemap.pmtiles.new" "$TILESETS/static/tiles/basemap.pmtiles"
    rm -f "$TMPDIR_HOST/basemap.mbtiles"
    ls -lh "$TILESETS/static/tiles/basemap.pmtiles"
    ;;
  *)
    echo "usage: $0 planet|cfd|merge" >&2
    exit 1
    ;;
esac
