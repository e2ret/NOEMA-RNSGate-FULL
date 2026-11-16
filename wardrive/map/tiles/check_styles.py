#!/usr/bin/env python3
"""Verify every served style can actually be rendered by the assets on disk.

A GL style fails quietly: a source-layer that the tileset does not contain, or a
fontstack with no glyph directory, produces a map that is simply missing those
features rather than an error. This checks both up front.
"""
import json
import os
import subprocess
import sys

def tileset_layers(pmtiles_path):
    out = subprocess.run(["pmtiles", "show", pmtiles_path, "--metadata"],
                         capture_output=True, text=True).stdout
    meta = json.loads(out)
    return {v["id"] for v in meta.get("vector_layers", [])}

def main():
    static = sys.argv[1] if len(sys.argv) > 1 else "/mnt/sdb1/tilesets/static"
    pmtiles = os.path.join(static, "tiles", "basemap.pmtiles")
    have_layers = tileset_layers(pmtiles)
    have_fonts = set(os.listdir(os.path.join(static, "fonts")))

    failures = 0
    for name in sorted(os.listdir(os.path.join(static, "styles"))):
        if not name.endswith(".json"):
            continue
        style = json.load(open(os.path.join(static, "styles", name)))
        need_layers, need_fonts = set(), set()
        for layer in style["layers"]:
            if "source-layer" in layer:
                need_layers.add(layer["source-layer"])
            font = layer.get("layout", {}).get("text-font")
            if isinstance(font, list) and all(isinstance(f, str) for f in font):
                need_fonts.add(",".join(font))

        missing_layers = sorted(need_layers - have_layers)
        missing_fonts = sorted(need_fonts - have_fonts)
        sprite_dir = os.path.join(static, style["sprite"].lstrip("/").rsplit("/", 1)[0])
        sprite_ok = os.path.isfile(os.path.join(sprite_dir, "sprite.json"))

        ok = not missing_layers and not missing_fonts and sprite_ok
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {name:26s} "
              f"layers {len(need_layers)}  fonts {len(need_fonts)}  "
              f"sprite {'ok' if sprite_ok else 'MISSING'}")
        for m in missing_layers:
            print(f"        missing source-layer: {m}")
        for m in missing_fonts:
            print(f"        missing fontstack:    {m}")
    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())
