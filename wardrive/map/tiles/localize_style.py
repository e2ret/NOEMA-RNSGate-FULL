#!/usr/bin/env python3
"""Rewrite an OpenMapTiles GL style to reference only self-hosted assets.

Upstream styles point at api.maptiler.com for both tiles and glyphs (with an API
key placeholder) and at a GitHub Pages URL for sprites. All three are replaced so
the map makes no third-party requests at runtime.

The vector source becomes a pmtiles:// URL -- pmtiles.js registers that protocol
client-side and turns it into HTTP range requests against the single archive file,
so nginx serves the whole basemap as one static file with no tile server.
"""
import json
import sys

SOURCE_NAME = "openmaptiles"

def localize(style, pmtiles_url, glyphs_url, sprite_url):
    style["sources"] = {
        SOURCE_NAME: {
            "type": "vector",
            "url": f"pmtiles://{pmtiles_url}",
        }
    }
    style["glyphs"] = glyphs_url
    style["sprite"] = sprite_url

    # The upstream style carries MapTiler/Mapbox provenance that no longer describes
    # this build; drop it so the served style does not advertise a source it never hits.
    for key in ("openmaptiles:mapbox:source:url", "mapbox:autocomposite"):
        style.get("metadata", {}).pop(key, None)

    remaining = [j for j in json.dumps(style).split('"')
                 if j.startswith("http") and "maptiler" in j.lower()]
    if remaining:
        raise SystemExit(f"style still references maptiler: {remaining}")
    return style

def main():
    src = sys.argv[1]
    dst = sys.argv[2]
    pmtiles_url = sys.argv[3] if len(sys.argv) > 3 else "/tiles/basemap.pmtiles"
    glyphs_url = sys.argv[4] if len(sys.argv) > 4 else "/fonts/{fontstack}/{range}.pbf"
    sprite_url = sys.argv[5] if len(sys.argv) > 5 else "/sprite/sprite"

    with open(src) as fh:
        style = json.load(fh)
    style = localize(style, pmtiles_url, glyphs_url, sprite_url)
    with open(dst, "w") as fh:
        json.dump(style, fh, separators=(",", ":"))

    print(f"wrote {dst}")
    print(f"  layers : {len(style['layers'])}")
    print(f"  source : {style['sources'][SOURCE_NAME]['url']}")
    print(f"  glyphs : {style['glyphs']}")
    print(f"  sprite : {style['sprite']}")

if __name__ == "__main__":
    main()
