#!/usr/bin/env python3
"""Make *-xyz.json copies of the map.rns.moscow styles that read plain per-tile URLs instead of pmtiles://.

Usage: make_xyz_styles.py STYLES_DIR [OUT_DIR]      (OUT_DIR defaults to STYLES_DIR)
Reads <name>.json for each basemap style, writes <name>-xyz.json. glyphs/sprite become absolute too,
so the file is self-contained for MapLibre's offline downloader.
"""
import json, os, sys

ORIGIN = "https://map.rns.moscow"
TILES = ORIGIN + "/tiles/z/basemap/{z}/{x}/{y}.mvt"
NAMES = ["rns-dark", "positron", "dark-matter", "voyager", "osm-bright"]

src_dir = sys.argv[1]
out_dir = sys.argv[2] if len(sys.argv) > 2 else src_dir
for name in NAMES:
    path = os.path.join(src_dir, name + ".json")
    with open(path, encoding="utf-8") as f:
        style = json.load(f)
    for key, src in (style.get("sources") or {}).items():
        url = src.get("url")
        if isinstance(url, str) and url.startswith("pmtiles://"):
            src.pop("url")
            src["tiles"] = [TILES]
            src.setdefault("minzoom", 0)
            src.setdefault("maxzoom", 14)
    for key in ("glyphs", "sprite"):
        val = style.get(key)
        if isinstance(val, str) and val.startswith("/"):
            style[key] = ORIGIN + val
    out = os.path.join(out_dir, name + "-xyz.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(style, f, ensure_ascii=False, separators=(",", ":"))
    print("wrote", out)
