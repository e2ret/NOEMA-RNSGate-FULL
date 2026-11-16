#!/usr/bin/env python3
"""Check that the JavaScript inside MAP_HTML still parses.

MAP_HTML is one large non-raw Python string, so every backslash in it is a Python
escape before it is ever JavaScript. A stray \\n inside a JS comment becomes a real
newline and splits the comment, leaving its second half as bare code; the whole
inline <script> then fails to parse and the page renders with no map at all, while
the server looks perfectly healthy. That has happened three times -- once with \\d in
a regex, once with \\u{...} in a string, once with \\n in a comment.

Reads the literal the way Python will (so escapes are applied identically), pulls out
the inline script, and hands it to `node --check`.

    python3 map/check_page_js.py [path/to/map_server.py]
"""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile

_OSM = json.dumps({"provider": "osm", "base": "", "raster": {
    "label": "OpenStreetMap", "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "attribution": "OSM", "max_zoom": 19, "tile_size": 256}})

def extract_map_html(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "MAP_HTML":
                    return ast.literal_eval(node.value)
    sys.exit("MAP_HTML not found")

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "map_server.py")
    html = extract_map_html(path)

    # Jinja placeholders are not valid JS; substitute something harmless so the parse
    # tests our code rather than the templating.
    # The basemap provider is the exception: it selects whole branches of the script,
    # so the stub supplies it per run instead (0 falls through to the local provider).
    html = re.sub(r"\{\{\s*basemap\s*\|\s*safe\s*\}\}", "(globalThis.__BASEMAP__ || 0)", html)
    # Likewise the community overlay flag: it gates a whole block of the script, and the
    # generic substitution below would leave that block permanently switched off.
    html = re.sub(r"\{\{\s*'true' if overlay_on else 'false'\s*\}\}",
                  "(globalThis.__COMMUNITY__ ? true : false)", html)
    html = re.sub(r"\{\{[^}]*\}\}", "0", html)

    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    if not blocks:
        sys.exit("no inline <script> found in MAP_HTML")

    failed = 0
    for i, js in enumerate(blocks):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(js)
            tmp = f.name
        try:
            r = subprocess.run(["node", "--check", tmp],
                               capture_output=True, text=True)
        finally:
            os.unlink(tmp)
        if r.returncode:
            failed += 1
            print(f"FAIL  block {i} ({len(js)} chars)")
            print(r.stderr.rstrip())
            continue
        print(f"PASS  block {i} ({len(js)} chars) parses")

        # Parsing is not running. Execute it against a stub DOM, in each of the three
        # shapes the page actually loads in, so a temporal-dead-zone reference or any
        # other start-up throw is caught here rather than by a blank page.
        stub = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page_stub.mjs")
        if not os.path.exists(stub):
            print("WARN  page_stub.mjs missing; skipped the run-time check")
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(js)
            tmp = f.name
        try:
            for label, env in (("standalone/en", {}),
                               ("embedded/en", {"EMBED": "1"}),
                               ("standalone/ru", {"NAVLANG": "ru-RU"}),
                               ("remote/en", {"BASEMAP": json.dumps(
                                   {"provider": "remote", "base": "https://tiles.example",
                                    "raster": None})}),
                               ("osm/en", {"BASEMAP": _OSM}),
                               ("osm/embedded", {"BASEMAP": _OSM, "EMBED": "1"}),
                               ("community/en", {"COMMUNITY": "1"}),
                               ("community/ru", {"COMMUNITY": "1", "NAVLANG": "ru-RU"})):
                r = subprocess.run(["node", stub, tmp], capture_output=True, text=True,
                                   env={**os.environ, **env})
                first = (r.stdout or r.stderr).strip().splitlines()
                head = first[0] if first else "(no output)"
                if r.returncode:
                    failed += 1
                    print(f"FAIL  block {i} run-time [{label}]")
                    print("      " + "\n      ".join(first[:6]))
                else:
                    print(f"PASS  block {i} run-time [{label}]  {head}")
        finally:
            os.unlink(tmp)
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
