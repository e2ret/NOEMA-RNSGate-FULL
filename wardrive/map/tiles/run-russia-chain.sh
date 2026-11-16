#!/bin/bash
# Download Russia, build z13-14, merge with the existing planet z0-12, swap it in.
#
# Deliberately not `set -e` around the phases: each is checked and logged so a failure
# names itself in chain-russia.log rather than the script vanishing silently.
TILESETS=/mnt/sdb1/tilesets
SRC=$TILESETS/src/russia-latest.osm.pbf
LOG=$TILESETS/chain-russia.log
BUILD=/root/Development/rns-wardrive-tools/map/tiles/build.sh

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

log "=== phase 0: download russia-latest.osm.pbf ==="
# -c resumes a partial file; the URL is stable and Geofabrik supports ranges.
wget -c -q -O "$SRC" https://download.geofabrik.de/russia-latest.osm.pbf \
     >> "$TILESETS/russia-dl.log" 2>&1
if [ $? -ne 0 ]; then log "ABORT: download failed"; exit 1; fi
log "downloaded: $(stat -c%s "$SRC") bytes"

log "verifying md5"
curl -s https://download.geofabrik.de/russia-latest.osm.pbf.md5 -o "$SRC.md5"
# Geofabrik's file names the basename, so verify from that directory.
( cd "$(dirname "$SRC")" && md5sum -c "$(basename "$SRC").md5" ) >> "$LOG" 2>&1
if [ $? -ne 0 ]; then log "ABORT: md5 mismatch"; exit 1; fi
log "md5 OK"

log "=== phase 1: russia z13-14 ==="
HEAP=8g "$BUILD" russia >> "$TILESETS/build-russia.log" 2>&1
if [ $? -ne 0 ]; then log "ABORT: russia build failed, see build-russia.log"; exit 1; fi
log "russia z13-14 done: $(du -h "$TILESETS/out/russia-z1314.mbtiles" | cut -f1)"

log "=== phase 2: merge + convert + swap ==="
"$BUILD" merge >> "$TILESETS/build-merge-russia.log" 2>&1
if [ $? -ne 0 ]; then log "ABORT: merge failed, see build-merge-russia.log"; exit 1; fi
log "DONE: basemap.pmtiles is $(du -h "$TILESETS/static/tiles/basemap.pmtiles" | cut -f1)"
