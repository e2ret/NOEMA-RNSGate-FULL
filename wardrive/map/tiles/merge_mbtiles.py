#!/usr/bin/env python3
"""Merge Planetiler mbtiles archives that cover disjoint zoom ranges.

The basemap is built in two passes -- the planet at low/mid zoom and a regional extract
at high zoom -- because rendering the whole planet to z14 costs far more disk and time
than this map needs. This stitches them back into one archive.

The zoom ranges must not overlap. Where they do, both inputs hold a full copy of the
same tile, and concatenating them would put two copies of every feature in it; the
check below refuses that case rather than producing a subtly doubled map.

Reads through the `tiles` relation, which is a real table in Planetiler's plain output
and a compatibility view in its compact/deduplicated output, so either schema works.
"""
import os
import sqlite3
import sys

def zoom_range(con):
    row = con.execute("SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles").fetchone()
    return row if row and row[0] is not None else (None, None)

def read_metadata(con):
    try:
        return dict(con.execute("SELECT name, value FROM metadata").fetchall())
    except sqlite3.Error:
        return {}

def merge(inputs, out_path):
    if os.path.exists(out_path):
        os.remove(out_path)

    srcs = []
    for path in inputs:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        lo, hi = zoom_range(con)
        if lo is None:
            sys.exit(f"{path}: contains no tiles")
        srcs.append((path, con, lo, hi))
        print(f"  {os.path.basename(path)}: z{lo}-{hi}")

    srcs.sort(key=lambda s: s[2])
    for (pa, _, _, hi_a), (pb, _, lo_b, _) in zip(srcs, srcs[1:]):
        if lo_b <= hi_a:
            sys.exit(f"zoom ranges overlap: {os.path.basename(pa)} ends at z{hi_a}, "
                     f"{os.path.basename(pb)} starts at z{lo_b}")

    out = sqlite3.connect(out_path)
    out.execute("PRAGMA journal_mode=OFF")
    out.execute("PRAGMA synchronous=OFF")
    out.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    out.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                "tile_row INTEGER, tile_data BLOB)")

    total = 0
    for path, con, _, _ in srcs:
        n = 0
        cur = con.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles")
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            out.executemany("INSERT INTO tiles VALUES (?,?,?,?)", batch)
            n += len(batch)
        out.commit()
        total += n
        print(f"  copied {n} tiles from {os.path.basename(path)}")

    # Metadata comes from the deepest archive (its vector_layers describe the most
    # detailed zooms), with the zoom span widened to cover everything merged in.
    meta = read_metadata(srcs[-1][1])
    meta["minzoom"] = str(srcs[0][2])
    meta["maxzoom"] = str(srcs[-1][3])
    meta["format"] = meta.get("format", "pbf")

    # Bounds must be the union, not the deepest archive's. Inheriting them along with
    # everything else described a planet-wide merge with the extent of its smallest
    # input, and that is not cosmetic: pmtiles convert copies bounds into the archive
    # header, pmtiles.js reports the header bounds to MapLibre as the source's TileJSON,
    # and MapLibre refuses to request tiles outside a source's bounds. The merged planet
    # tiles were present and unreachable.
    boxes = []
    for path, con, _, _ in srcs:
        raw = read_metadata(con).get("bounds")
        if not raw:
            continue
        try:
            boxes.append([float(x) for x in raw.split(",")])
        except ValueError:
            print(f"  WARNING: unparseable bounds in {os.path.basename(path)}: {raw!r}")
    if boxes:
        union = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                 max(b[2] for b in boxes), max(b[3] for b in boxes)]
        meta["bounds"] = ",".join(f"{v:g}" for v in union)
        print(f"  bounds union: {meta['bounds']}")
    out.executemany("INSERT INTO metadata VALUES (?,?)", sorted(meta.items()))

    print("  indexing...")
    out.execute("CREATE UNIQUE INDEX tile_index ON tiles "
                "(zoom_level, tile_column, tile_row)")
    out.commit()
    out.close()
    for _, con, _, _ in srcs:
        con.close()

    print(f"wrote {out_path}: {total} tiles, z{meta['minzoom']}-{meta['maxzoom']}, "
          f"{os.path.getsize(out_path) / 1e9:.1f} GB")

if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit("usage: merge_mbtiles.py OUT.mbtiles IN1.mbtiles IN2.mbtiles [...]")
    merge(sys.argv[2:], sys.argv[1])
