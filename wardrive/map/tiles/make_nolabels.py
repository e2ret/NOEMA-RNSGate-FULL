#!/usr/bin/env python3
"""Derive a label-free variant of a GL style.

The map previously offered a "Voyager (no labels)" raster basemap, which is useful
here because the node markers carry their own permanent labels and a labelled basemap
competes with them. Vector styles make this a filter rather than a separate tileset:
dropping the symbol layers removes place names, road shields and POI text while
leaving all the geometry intact.
"""
import json
import sys

def strip_labels(style):
    kept = [l for l in style["layers"] if l.get("type") != "symbol"]
    dropped = len(style["layers"]) - len(kept)
    style["layers"] = kept
    style["name"] = style.get("name", "") + " (no labels)"
    return style, dropped

def main():
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as fh:
        style = json.load(fh)
    style, dropped = strip_labels(style)
    with open(dst, "w") as fh:
        json.dump(style, fh, separators=(",", ":"))
    print(f"wrote {dst}: dropped {dropped} symbol layers, {len(style['layers'])} remain")

if __name__ == "__main__":
    main()
