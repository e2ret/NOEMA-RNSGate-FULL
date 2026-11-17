#!/opt/reticulum/bin/python3
"""
map_server.py - Live LoRa coverage heatmap for wardrive_bot.

Serves a MapLibre GL heatmap that polls for new points every 5 seconds.
Reads from the same SQLite database written by wardrive_bot.py. The database is
optional: without it the map is a viewer for the GeoJSON/CSV/GPX files exported by the
rns-wardrive Android app (opened in the browser, nothing is uploaded).
"""

import gzip
import hashlib
from collections import Counter
import json
import math
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request

from flask import Flask, jsonify, make_response, render_template_string, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

CONFIG_PATH = "/etc/reticulum/wardrive_map.json"

def _load_config(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[WARN] Could not load config from {path}: {e}")
        return {}

_cfg = _load_config(CONFIG_PATH)

DB_PATH       = os.path.expanduser(_cfg.get("db_path",       "~/wardrive_bot.db"))
GATEWAY_POSITION_PATH = _cfg.get(
    "gateway_position_path",
    os.path.join(os.path.expanduser("~"), "dashboard", "netmap.json"),
)
NODE_PRUNE_AGE = _cfg.get("node_prune_age_secs", 7 * 24 * 3600)  # default 7 days
DISCOVERY_REFRESH_SECS = _cfg.get("discovery_refresh_secs", 15)  # background rnstatus poll
# nginx proxies to 127.0.0.1:8840 and serves the basemap assets itself, so the app has
# no reason to be reachable off-box. Binding 0.0.0.0 exposed a second, plaintext path to
# the same app that bypassed TLS and the /tiles /fonts /sprite /static aliases.
HOST          = _cfg.get("listen_on",     "127.0.0.1")
PORT          = _cfg.get("port",          5001)
SERVER_THREADS = _cfg.get("server_threads", 8)
PAGE_TITLE    = _cfg.get("page_title",    "Wardrive Coverage Map")
INITIAL_LAT   = _cfg.get("initial_lat",   None)
INITIAL_LON   = _cfg.get("initial_lon",   None)
INITIAL_ZOOM  = _cfg.get("initial_zoom",  None)
DEFAULT_COUNTRY = _cfg.get("default_country_filter", "Russia")
PINNED_NODES  = _cfg.get("pinned_nodes",  [])
# Basemap assets (vector tiles, glyphs, sprites, JS) live outside the code tree because
# the tileset is far too large to version. nginx serves these paths directly in
# production; the Flask routes below are the fallback that keeps the map working when
# hitting the port directly, e.g. for local testing.
TILESET_DIR = _cfg.get("tileset_dir", "/mnt/sdb1/tilesets")
STATIC_DIR = os.path.join(TILESET_DIR, "static")

# Where the basemap comes from. "local" is the tileset above; the others exist so an
# install does not have to build 22 GB of tiles to show a map:
#   remote  styles, tiles, glyphs and sprites fetched by the browser straight from
#           another install that has public_url set (basemap_url names it)
#   osm     OpenStreetMap's raster tiles
#   raster  any XYZ raster tile server, described by raster_tiles
BASEMAP_PROVIDER = _cfg.get("basemap_provider", "local")
BASEMAP_URL = (_cfg.get("basemap_url") or "").rstrip("/")
OSM_RASTER = {
    "label":       "OpenStreetMap",
    "url":         "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "attribution": '&copy; <a href="https://www.openstreetmap.org/copyright">'
                   'OpenStreetMap</a> contributors',
    "max_zoom":    19,
    "tile_size":   256,
}
if BASEMAP_PROVIDER == "osm":
    RASTER_TILES = dict(OSM_RASTER)
elif BASEMAP_PROVIDER == "raster":
    _raster = _cfg.get("raster_tiles") or {}
    if not _raster.get("url"):
        raise SystemExit('[CONFIG] basemap_provider "raster" needs raster_tiles.url')
    RASTER_TILES = {"label": "Map", "attribution": "", "max_zoom": 19, "tile_size": 256,
                    **_raster}
elif BASEMAP_PROVIDER == "remote":
    if not BASEMAP_URL:
        raise SystemExit('[CONFIG] basemap_provider "remote" needs basemap_url')
    RASTER_TILES = None
elif BASEMAP_PROVIDER == "local":
    RASTER_TILES = None
else:
    raise SystemExit(f'[CONFIG] unknown basemap_provider "{BASEMAP_PROVIDER}" '
                     '(expected local, remote, osm or raster)')
# Vector providers need pmtiles.js and the style files; raster ones need neither.
BASEMAP_IS_RASTER = RASTER_TILES is not None

# Set on an install that lets others use it as their "remote" provider. Styles then
# carry absolute URLs -- a relative /tiles/... would resolve against the borrowing
# page's origin, not this one -- and the asset routes answer cross-origin requests.
# Unset, nothing about the responses changes.
PUBLIC_URL = (_cfg.get("public_url") or "").rstrip("/")

# MapLibre and pmtiles.js ship beside this file so a remote or raster install needs no
# tileset directory at all. A local install keeps loading them from /static/js, which
# nginx serves off disk.
VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
JS_BASE = "/static/js" if BASEMAP_PROVIDER == "local" else "/vendor"

GEO_CACHE_PATH = _cfg.get("geo_cache_path",
                           os.path.join(os.path.dirname(DB_PATH), "wardrive_geo_cache.json"))

def _load_geo_cache():
    try:
        with open(GEO_CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_geo_cache():
    try:
        existed = os.path.exists(GEO_CACHE_PATH)
        with _geo_lock:
            data = dict(_geo_cache)
        with open(GEO_CACHE_PATH, "w") as f:
            json.dump(data, f)
        action = "updated" if existed else "created"
        print(f"[GEO] cache {action}: {GEO_CACHE_PATH} ({len(data)} entries)", flush=True)
    except Exception as e:
        print(f"[GEO] cache save failed: {e}", flush=True)

_geo_cache = _load_geo_cache()
_geo_lock  = threading.Lock()
_geo_busy  = [False]

def _geo_key(lat, lon):
    return f"{round(lat, 2):.2f},{round(lon, 2):.2f}"

# Reverse geocoding runs against a local GeoNames extract (see map/geo/build_geodb.py)
# rather than api.bigdatacloud.net. Nodes are positioned by IP geolocation, which
# already resolves to city-level coordinates, so nearest-populated-place agrees with
# the old service's answers in practice while removing the third-party dependency.
GEO_DB_PATH = _cfg.get("geo_db_path", os.path.join(TILESET_DIR, "geo", "cities.tsv.gz"))
# Beyond this the nearest city says nothing useful about where a point actually is, so
# report unknown rather than attach a place name hundreds of km away.
GEO_MAX_KM = _cfg.get("geo_max_km", 200)

_geo_db = None

def _load_geo_db():
    """Load places into parallel arrays plus a 1-degree bucket index."""
    lats, lons, places, grid = [], [], [], {}
    with gzip.open(GEO_DB_PATH, "rt", encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 5:
                continue
            lat, lon = float(parts[0]), float(parts[1])
            grid.setdefault((math.floor(lat), math.floor(lon)), []).append(len(lats))
            lats.append(lat)
            lons.append(lon)
            # Columns past the country name are optional, so a database built before
            # they existed still loads; those rows fall back to the English names.
            def col(i):
                return parts[i] if len(parts) > i else ""
            places.append((parts[2], parts[3], parts[4], col(5), col(6), col(7), col(8)))
    print(f"[GEO] loaded {len(lats)} places from {GEO_DB_PATH}", flush=True)
    return lats, lons, places, grid

_geo_db_missing = False

def _get_geo_db():
    """The places database, or None when the install has not built one.

    A remote or raster install has no tileset directory, so the default path does not
    exist. That is reported once and places stay unknown, rather than raising on every
    node of every refresh.
    """
    global _geo_db, _geo_db_missing
    if _geo_db is None and not _geo_db_missing:
        try:
            _geo_db = _load_geo_db()
        except FileNotFoundError:
            _geo_db_missing = True
            print(f"[GEO] no places database at {GEO_DB_PATH}; node locations will be "
                  "unnamed (build one with map/geo/build_geodb.py)", flush=True)
    return _geo_db

def _km(lat1, lon1, lat2, lon2):
    """Equirectangular approximation -- accurate enough at the scale we compare."""
    mean = math.radians((lat1 + lat2) / 2)
    x = math.radians(lon2 - lon1) * math.cos(mean)
    y = math.radians(lat2 - lat1)
    return 6371.0 * math.hypot(x, y)

def _nearest_place(lat, lon):
    lats, lons, places, grid = _get_geo_db()
    clat, clon = math.floor(lat), math.floor(lon)
    best, best_km = None, None
    # Widening ring search. A hit is only trusted once the searched box is provably
    # wider than the best distance found, otherwise a nearer place could sit just
    # outside it -- so keep expanding until that holds.
    for r in (1, 2, 4, 8):
        for dlat in range(-r, r + 1):
            for dlon in range(-r, r + 1):
                lon_cell = clon + dlon
                if lon_cell > 179:
                    lon_cell -= 360
                elif lon_cell < -180:
                    lon_cell += 360
                for i in grid.get((clat + dlat, lon_cell), ()):
                    d = _km(lat, lon, lats[i], lons[i])
                    if best_km is None or d < best_km:
                        best, best_km = i, d
        if best is not None:
            safe = (r - 1) * 111.0 * max(0.1, math.cos(math.radians(lat)))
            if best_km <= safe:
                break
    if best is None or best_km > GEO_MAX_KM:
        return None
    return places[best]

def _fetch_geo(lat, lon):
    key = _geo_key(lat, lon)
    with _geo_lock:
        if key in _geo_cache:
            return
    try:
        # Checked before the lookup so nothing is cached: a miss recorded now would
        # outlive the database being installed later.
        if _get_geo_db() is None:
            return
        place = _nearest_place(lat, lon)
        # Cache misses too: the dataset is fixed, so a miss will not become a hit on a
        # later attempt and retrying would rescan on every refresh.
        geo = {"country": "", "state": "", "city": "", "cc": ""}
        if place:
            city, state, country, cc, city_loc, state_loc, loc_lang = place
            geo = {"country": country, "state": state, "city": city, "cc": cc}
            # Local-language names travel under the language they are in, so the page can
            # use them only when it is showing that language.
            if loc_lang and (city_loc or state_loc):
                geo["loc_lang"]  = loc_lang
                geo["city_loc"]  = city_loc
                geo["state_loc"] = state_loc
        with _geo_lock:
            _geo_cache[key] = geo
    except Exception as e:
        print(f"[GEO] {lat},{lon}: {type(e).__name__}: {e}", flush=True)

def _geocode_bg(nodes):
    try:
        seen = set()
        for n in nodes:
            key = _geo_key(n["lat"], n["lon"])
            if key in seen:
                continue
            seen.add(key)
            with _geo_lock:
                if key in _geo_cache:
                    continue
            _fetch_geo(n["lat"], n["lon"])
    except Exception as e:
        print(f"[GEO] thread error: {e}", flush=True)
    finally:
        _geo_busy[0] = False
        _save_geo_cache()

def _enrich_geo(nodes):
    uncached = []
    for n in nodes:
        key = _geo_key(n["lat"], n["lon"])
        with _geo_lock:
            geo = _geo_cache.get(key)
        if geo:
            n["country"] = geo["country"]
            n["state"]   = geo["state"]
            n["city"]    = geo["city"]
            # Missing from cache entries written before these were recorded.
            n["cc"]        = geo.get("cc", "")
            n["loc_lang"]  = geo.get("loc_lang", "")
            n["city_loc"]  = geo.get("city_loc", "")
            n["state_loc"] = geo.get("state_loc", "")
        else:
            n["country"] = n["state"] = n["city"] = n["cc"] = ""
            n["loc_lang"] = n["city_loc"] = n["state_loc"] = ""
            uncached.append(n)
    if uncached and not _geo_busy[0]:
        _geo_busy[0] = True
        threading.Thread(target=_geocode_bg, args=(uncached,), daemon=True).start()

# Community overlay: signal reports uploaded by invited contributors (map/overlay_*.py).
# Off unless overlay_enabled is true -- an install that only views exports has no reason
# to grow a write surface. Tunables go under "overlay": the limits in overlay_store.DEFAULTS
# (max_rows_day, max_batches_day, max_invites, invite_ttl_days, max_total_rows) and the
# ones in overlay_validate.Limits (max_rows, max_age_days, max_speed_ms, ...).
OVERLAY_ENABLED = bool(_cfg.get("overlay_enabled", False))
OVERLAY_DB_PATH = os.path.expanduser(_cfg.get(
    "overlay_db_path", os.path.join(os.path.dirname(DB_PATH), "wardrive_overlay.db")))
OVERLAY_MAX_UPLOAD = int(_cfg.get("overlay_max_upload_bytes", 4 * 1024 * 1024))
OVERLAY_MAX_RESPONSE_ROWS = 50000
# Imported only when enabled, so a deploy that copies map_server.py alone keeps working.
_overlay = None
_overlay_limits = None
if OVERLAY_ENABLED:
    import overlay_validate
    from overlay_store import OverlayStore, StoreError
    _ov_cfg = _cfg.get("overlay") or {}
    _overlay = OverlayStore(OVERLAY_DB_PATH, **_ov_cfg)
    _overlay_limits = overlay_validate.Limits(**{
        k: v for k, v in _ov_cfg.items() if k in overlay_validate.Limits.__dataclass_fields__})

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
# Werkzeug stops reading at this size; waitress gets its own cap in _serve().
app.config["MAX_CONTENT_LENGTH"] = OVERLAY_MAX_UPLOAD

MAP_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{{ page_title }}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="{{ js_base }}/maplibre-gl.css">
  <style>
    /* NOEMA chrome follows the dashboard's midnight and cyan palette.

       --accent-* use NOEMA cyan for the active controls. Node-type colours,
       heatmap legend gradients and the apply/clear pair are deliberately NOT themed:
       those encode meaning, and recolouring them would destroy the information. */
    :root {
      --bg:         #060a1a;
      --bg2:        #0c101e;
      --bg3:        #111a2b;
      --bg-entry:   #0b1222;
      --border:     rgba(56, 189, 248, 0.16);
      --border2:    rgba(56, 189, 248, 0.28);
      --border3:    rgba(56, 189, 248, 0.55);
      --text:       #cfe8f5;
      --text-dim:   #8eafc0;
      --text-muted: #4e7988;
      --text-faint: #3c5b70;
      --text-hash:  #78cce2;
      --mask-bg:    #060a1a;
      --tip-bg:     #0c101e;
      --accent:        #38bdf8;
      --accent-bright: #5ccbfa;
      --accent-ghost:  rgba(56, 189, 248, 0.10);
      --accent-ghost2: rgba(56, 189, 248, 0.20);
      --accent-dim:    rgba(56, 189, 248, 0.55);
      color-scheme: dark;
    }
    body.light-theme {
      --bg:         #fff;
      --bg2:        #f4f4f4;
      --bg3:        #e8e8e8;
      --bg-entry:   #eaeaea;
      --border:     #ddd;
      --border2:    #bbb;
      --border3:    #999;
      --text:       #222;
      --text-dim:   #555;
      --text-muted: #888;
      --text-faint: #aaa;
      --text-hash:  #999;
      --mask-bg:    #fff;
      --tip-bg:     #f5f5f5;
      --accent:        #007ca8;
      --accent-bright: #009dcc;
      --accent-ghost:  rgba(0, 124, 168, 0.10);
      --accent-ghost2: rgba(0, 124, 168, 0.20);
      --accent-dim:    rgba(0, 124, 168, 0.65);
      color-scheme: light;
    }
    * { box-sizing: border-box; }
    html { height: 100%; }
    body { height: 100%; margin: 0; padding: 0; position: relative; overflow: hidden; background: var(--bg2); }
    #map { position: absolute; inset: 54px 0 0; min-width: 0; }
    #map-topbar {
      position: fixed; inset: 0 0 auto; min-height: 54px; z-index: 620;
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
      padding: 8px 12px; background: var(--bg); border-bottom: 1px solid var(--border);
    }
    .map-topbar-group { display: flex; align-items: center; gap: 6px; min-width: 0; flex-wrap: wrap; }
    /* OpenMapTiles is CC-BY and OSM data is ODbL: both require visible credit, so
       unlike the old CARTO raster layers the attribution cannot simply be hidden.
       Compact mode keeps it to a single small button until clicked. */
    .maplibregl-ctrl-logo { display: none; }
    .maplibregl-ctrl-attrib {
      background: var(--bg2) !important; color: var(--text-dim) !important;
      font: 10px/1.4 monospace; border-radius: 3px 0 0 0;
    }
    .maplibregl-ctrl-attrib-inner,
    .maplibregl-ctrl-attrib a,
    .maplibregl-ctrl-attrib button { color: var(--text-dim) !important; }
    .maplibregl-ctrl-attrib a:hover { color: var(--text) !important; }
    .maplibregl-ctrl-attrib-button { background-color: var(--bg2) !important; color: var(--text-dim) !important; }
    #follow-control {
      position: absolute; top: 10px; left: 10px; z-index: 400;
      display: flex; flex-direction: column; gap: 6px;
    }
    .follow-btn {
      display: block; width: 30px; height: 30px; padding: 0;
      background: var(--bg2); color: var(--text);
      border: 1px solid var(--border2); border-radius: 4px;
      font-size: 15px; line-height: 28px; text-align: center; cursor: pointer;
      box-shadow: 0 1px 4px rgba(0,0,0,0.3);
    }
    .follow-btn:hover { background: var(--bg3); color: var(--text); }
    .follow-btn.active { background: var(--accent-ghost); color: var(--accent); border-color: var(--accent-dim); }
    .follow-btn.active:hover { background: var(--accent-ghost2); color: var(--accent-bright); }
    #ruler-readout {
      position: absolute; top: 10px; left: 50px; z-index: 400; display: none;
      align-items: center; gap: 8px; padding: 0 4px 0 10px;
      /* Always as tall as the 30px buttons beside it, whatever it says: two lines of 13px fit inside. */
      box-sizing: border-box; height: 30px;
      background: var(--bg2); color: var(--text); font: 11px/13px monospace; font-weight: bold;
      border: 1px solid var(--border2); border-radius: 4px; box-shadow: 0 1px 4px rgba(0,0,0,0.3);
    }
    .ruler-label, .ruler-arrow { pointer-events: none; }
    .ruler-label span {
      display: block; white-space: nowrap; padding: 1px 6px;
      background: var(--bg2); color: var(--text); border: 1px solid var(--border2); border-radius: 4px;
      font: 11px/1.4 monospace; font-weight: bold; box-shadow: 0 1px 4px rgba(0,0,0,0.3);
    }
    .rl-true { color: #64b5f6; }
    .rl-mag { color: #81c784; }
    body.light-theme .rl-true { color: #1565c0; }
    body.light-theme .rl-mag { color: #2e7d32; }
    /* A fixed width, so "first point" and "second point" and the distance/bearing lines all fit the same box. */
    #ruler-readout > div { min-width: 31ch; }
    #ruler-readout button {
      background: none; border: 0; color: var(--accent); font: inherit; cursor: pointer; padding: 2px 6px;
    }
    #panel {
      width: min(326px, calc(100vw - 24px)); display: flex !important; flex-direction: column;
      background: var(--bg); color: var(--text); font: 13px/1.4 monospace;
      border-left: 1px solid var(--border); position: fixed; right: 0; top: 54px; bottom: 0; z-index: 610;
    }
    #panel-header {
      padding: 10px 12px; font-size: 11px; font-weight: bold;
      letter-spacing: 0.08em; text-transform: uppercase;
      color: var(--text-dim); border-bottom: 1px solid var(--border); flex-shrink: 0;
      display: flex; align-items: center; justify-content: space-between;
    }
    .panel-btn {
      background: var(--bg2); border: 1px solid var(--border2); color: var(--text-dim);
      cursor: pointer; font-size: 12px; line-height: 1;
      padding: 2px 6px; border-radius: 3px;
    }
    .panel-btn:hover { background: var(--bg3); color: var(--text); }
    /* Beacon list stays open on the right; Stream / Node List buttons unused. */
    #panel-open-wrap, #panel-close, #nodes-open-wrap, #nodes-panel { display: none !important; }
    /* File import/share — not used for live phone beacons. */
    #file-open, #file-close, #file-publish, #file-input { display: none !important; }
    /* This gateway is RNode-only — hide other discovery transports. */
    #check-backbone, #check-ygg, #check-i2p, #check-halow,
    label.disc-item:has(#check-backbone),
    label.disc-item:has(#check-ygg),
    label.disc-item:has(#check-i2p),
    label.disc-item:has(#check-halow),
    #tab-dot-backbone, #tab-dot-i2p, #tab-dot-halow, #tab-dot-ygg {
      display: none !important;
    }
    /* Beacon heat map does not need the discovery-stale banner (rnstatus peers). */
    #discovery-warning { display: none !important; }
    .topbar-tab {
      display: inline-flex; align-items: center; gap: 7px; min-height: 34px;
      padding: 6px 10px; background: var(--bg2); color: var(--text-dim);
      border: 1px solid var(--border2); border-radius: 4px; cursor: pointer;
      font: 11px monospace; white-space: nowrap;
    }
    .topbar-tab:hover { background: var(--bg3); color: var(--text); border-color: var(--border3); }
    .topbar-tab.active { color: var(--accent); border-color: var(--accent-dim); background: var(--accent-ghost); }
    .entry-hash {
      color: var(--text-faint); cursor: pointer;
    }
    .entry-hash:hover { color: var(--text-hash); }
    #filter-bar {
      padding: 8px 12px; border-bottom: 1px solid var(--border); flex-shrink: 0;
      display: flex; flex-direction: column; gap: 6px;
    }
    #filter-dates { display: flex; align-items: center; gap: 6px; }
    .date-sep { color: var(--text-faint); }
    #filter-btns { display: flex; gap: 4px; }
    #filter-btns .panel-btn { flex: 1; }
    #filter-apply { background: rgba(56,189,248,.12); border-color: rgba(56,189,248,.5); color: #8edcff; }
    #filter-apply:hover { background: rgba(56,189,248,.2); color: #c8f1ff; }
    #filter-clear { background: #3a1a1a; border-color: #7a3a3a; color: #c77; }
    #filter-clear:hover { background: #4a1f1f; color: #e99; }
body.light-theme #filter-apply { background: #e8f6fb; border-color: #4a8ab0; color: #176287; }
body.light-theme #filter-apply:hover { background: #d5edf7; color: #104d6b; }
    body.light-theme #filter-clear { background: #f5e8e8; border-color: #8a4a4a; color: #6a2a2a; }
    body.light-theme #filter-clear:hover { background: #ecd5d5; color: #5a1a1a; }
    .date-input {
      background: var(--bg2); border: 1px solid var(--border2); color: var(--text-dim);
      font: 11px monospace; padding: 2px 4px; border-radius: 3px; flex: 1;
    }
    .date-input:focus { outline: none; border-color: var(--border3); color: var(--text); }
    #panel-list {
      flex: 1; overflow-y: auto; padding: 6px 0;
    }
    .entry {
      padding: 7px 12px; border-bottom: 1px solid var(--bg-entry);
      font-size: 12px; line-height: 1.6;
    }
    .entry:hover { background: var(--bg2); }
    .entry-time { color: var(--text-muted); font-size: 11px; display: flex; align-items: center; gap: 4px; }
    .entry-from { color: var(--text-faint); }
    .ts { position: relative; cursor: default; }
    .maplibregl-canvas-container.maplibregl-interactive { cursor: default; }
    .maplibregl-canvas-container.maplibregl-interactive:active { cursor: grabbing; }
    .ts:hover { color: var(--text-dim); }
    .ts:hover::after {
      content: attr(data-date);
      position: absolute; bottom: calc(100% + 4px); left: 0;
      background: var(--tip-bg); border: 1px solid var(--border2); color: var(--text);
      padding: 3px 8px; border-radius: 4px; white-space: nowrap;
      font-size: 11px; font-family: monospace; pointer-events: none; z-index: 600;
    }
    .entry-coords { color: var(--text-dim); font-size: 11px; cursor: pointer; }
    .src-pill {
      font-size: 8px; font-family: monospace;
      padding: 1px 3px; border-radius: 3px; white-space: nowrap;
    }
    .src-ann   { background: #1a1a2e; color: #55a; border: 1px solid #33c; }
    .src-pkt   { background: #1a2e1a; color: #585; border: 1px solid #3a3; }
    .src-relay { background: #2e1a2e; color: #a5a; border: 1px solid #a3a; }
    .src-file  { background: #1a2a2e; color: #5aa; border: 1px solid #3aa; }
    .entry-coords:hover { color: var(--text); }
    .rf-label { font-size: 9px; color: var(--text-muted); margin-right: 3px; vertical-align: middle; }
    .rf-bar {
      display: inline-block; width: 58px; height: 13px;
      background: linear-gradient(to right, #c33, #cc3, #3a3);
      position: relative; vertical-align: middle;
      border: 1px solid #888;
    }
    .rf-bar-q {
      background: linear-gradient(to right, #1144aa, #11aadd);
    }
    .rf-mask {
      position: absolute; right: 0; top: 0; bottom: 0; background: var(--mask-bg);
    }
    .rf-text {
      position: absolute; inset: 0; display: flex; align-items: center;
      justify-content: center; font-size: 10px; color: #fff;
      text-shadow: 0 0 3px #000; font-weight: bold; z-index: 1;
    }
    #hover-tip {
      position: fixed; pointer-events: none; z-index: 700; display: none;
      background: var(--bg); border: 1px solid var(--border2); border-radius: 3px;
      padding: 4px 6px;
    }
    .hover-row { display: flex; gap: 4px; align-items: center; }
    .hover-row > .vbar-col { font-size: 7px; font-family: 'Courier New', monospace; white-space: nowrap; position: relative; top: 2px; }
    #hover-bars-dl.with-divider { margin-top: 3px; padding-top: 3px; border-top: 1px solid var(--border2); }
    .vbar-wrap {
      width: 4px; height: 18px; position: relative;
      background: linear-gradient(to top, #c33, #cc3, #3a3);
    }
    .vbar-mask { position: absolute; top: 0; width: 100%; background: var(--mask-bg); }
    .vbar-col { display: flex; flex-direction: column; align-items: center; gap: 1px; }
    .vbar-lbl { font-size: 7px; color: var(--text-muted); font-family: 'Courier New', monospace; position: relative; top: -2px; }
    #status {
      padding: 8px 12px; font-size: 11px; color: var(--text-muted);
      border-top: 1px solid var(--border); flex-shrink: 0;
      display: flex; justify-content: space-between;
    }
    .map-popup .maplibregl-popup-content {
      background: var(--bg); color: var(--text); border: 1px solid var(--border2);
      padding: 0; overflow: hidden; border-radius: 4px; box-shadow: 0 1px 6px rgba(0,0,0,0.4);
    }
    /* The tip is a CSS triangle: a zero-sized box with 10px transparent borders, where
       exactly one side is coloured to point at the marker. MapLibre drops the opposing
       border per anchor but leaves the perpendicular pair transparent, so colouring all
       four sides fills those in too and the triangle renders as a solid square. Colour
       only the side that actually faces the marker, per anchor. */
    .map-popup.maplibregl-popup-anchor-top .maplibregl-popup-tip,
    .map-popup.maplibregl-popup-anchor-top-left .maplibregl-popup-tip,
    .map-popup.maplibregl-popup-anchor-top-right .maplibregl-popup-tip {
      border-bottom-color: var(--bg);
    }
    .map-popup.maplibregl-popup-anchor-bottom .maplibregl-popup-tip,
    .map-popup.maplibregl-popup-anchor-bottom-left .maplibregl-popup-tip,
    .map-popup.maplibregl-popup-anchor-bottom-right .maplibregl-popup-tip {
      border-top-color: var(--bg);
    }
    .map-popup.maplibregl-popup-anchor-left .maplibregl-popup-tip {
      border-right-color: var(--bg);
    }
    .map-popup.maplibregl-popup-anchor-right .maplibregl-popup-tip {
      border-left-color: var(--bg);
    }
    /* Leaflet drew its tip as a rotated bordered square, so the arrow inherited the
       bubble's 1px outline. A border-triangle cannot carry a border of its own -- the
       triangle *is* the border -- so the outline is drawn from the shape's alpha with
       four hard (zero-blur) drop-shadows, one per direction. That is anchor-agnostic,
       so it stays correct for all eight tip positions. */
    .map-popup .maplibregl-popup-tip {
      filter: drop-shadow( 1px 0 0 var(--border2))
              drop-shadow(-1px 0 0 var(--border2))
              drop-shadow(0  1px 0 var(--border2))
              drop-shadow(0 -1px 0 var(--border2));
    }
    .map-popup .maplibregl-popup-close-button {
      color: var(--text-dim); font-size: 15px; padding: 0 5px; background: none; border: none;
    }
    .map-popup .maplibregl-popup-close-button:hover { color: var(--text); background: none; }
    .map-popup .popup-body { font: 11px monospace; padding: 4px 6px; min-width: 180px; }
    /* The close button is absolutely positioned in the top-right corner and sits over
       the content. Reserving its width on the title alone means the popup grows wide
       enough for both instead of the X landing on a long node name -- and the other
       lines, which the button cannot reach, still get the full width. */
    .map-popup .popup-title { display: inline-block; padding-right: 22px; }
    #nodes-panel {
      display: none; position: fixed; right: 0; top: 54px; bottom: 0;
      width: min(260px, calc(100vw - 24px)); z-index: 610; flex-direction: column;
      background: var(--bg); color: var(--text); font: 13px/1.4 monospace;
      border-left: 1px solid var(--border);
    }
    #nodes-search-bar {
      padding: 8px 10px; border-bottom: 1px solid var(--border); flex-shrink: 0;
    }
    #nodes-search {
      width: 100%; background: var(--bg2); border: 1px solid var(--border2);
      color: var(--text); font: 11px monospace; padding: 4px 6px;
      border-radius: 3px; outline: none;
    }
    #nodes-search:focus { border-color: var(--border3); }
    #nodes-search::placeholder { color: var(--text-muted); }
    #nodes-header {
      padding: 10px 12px; font-size: 11px; font-weight: bold;
      letter-spacing: 0.08em; text-transform: uppercase;
      color: var(--text-dim); border-bottom: 1px solid var(--border);
      flex-shrink: 0; display: flex; align-items: center; justify-content: space-between;
    }
    #nodes-list { flex: 1; overflow-y: auto; padding: 4px 0; }
    #nodes-status {
      padding: 8px 12px; font-size: 11px; color: var(--text-muted);
      border-top: 1px solid var(--border); flex-shrink: 0;
      display: flex; justify-content: space-between;
    }
    #nodes-geo-bar {
      padding: 6px 10px 8px; border-bottom: 1px solid var(--border); flex-shrink: 0;
    }
    .geo-select {
      width: 100%; background: var(--bg2); border: 1px solid var(--border2);
      color: var(--text-dim); font: 11px monospace; padding: 3px 5px;
      border-radius: 3px; cursor: pointer; margin-bottom: 4px;
    }
    .geo-select:last-child { margin-bottom: 0; }
    .geo-select:focus { outline: none; border-color: var(--border3); color: var(--text); }
    .node-entry {
      padding: 7px 12px; border-bottom: 1px solid var(--bg-entry); cursor: pointer;
    }
    .node-entry:hover { background: var(--bg2); }
    .node-entry-name { font-size: 12px; display: flex; align-items: center; gap: 6px; justify-content: space-between; }
    .node-entry-name-left { display: flex; align-items: center; gap: 6px; }
    .node-entry-type { font-size: 10px; color: var(--text-muted); }
    .node-entry-meta { font-size: 10px; color: var(--text-dim); margin-top: 2px; }
    .node-entry-bottom { display: flex; justify-content: space-between; align-items: flex-end; margin-top: 1px; }
    .node-entry-coords { font-size: 10px; color: var(--text-faint); }
    .node-entry-age { font-size: 10px; color: var(--text-faint); }
    #discovery-container {
      position: fixed; left: 0; top: 50%; transform: translateY(-50%);
      z-index: 599;
    }
    #discovery-open-wrap {
      cursor: pointer;
      background: var(--border2);
      clip-path: polygon(0 0, 100% 8px, 100% calc(100% - 8px), 0 100%);
    }
    #discovery-open {
      display: block; writing-mode: vertical-rl;
      background: var(--bg); border: none;
      color: var(--text-dim); cursor: pointer; font: 11px monospace;
      padding: 12px 4px;
      clip-path: polygon(0 1px, calc(100% - 1px) 9px, calc(100% - 1px) calc(100% - 9px), 0 calc(100% - 1px));
    }
    #discovery-open-wrap:hover #discovery-open { background: var(--bg2); color: var(--text); }
    /* Inline SVG icons that replace emoji: sized to the text, coloured by it. */
    .ico { width: 1.2em; height: 1.2em; vertical-align: -0.2em; flex: none; }
    .tab-dot { display: none; width: 6px; height: 6px; border-radius: 50%; margin-top: 4px; }
    #discovery-options {
      padding: 6px 10px 8px; border-top: 1px solid var(--bg3);
    }
    /* No position here: .maplibregl-marker already sets position:absolute, and an
       equal-specificity rule declared after maplibre-gl.css would override it and
       drop every marker back into document flow. It is also what the label below
       positions against. */
    .node-marker { line-height: 0; cursor: pointer; }
    .node-dot { border-radius: 50%; box-sizing: border-box; }
    .node-label {
      position: absolute; z-index: 1;
      background: rgba(6, 10, 26, 0.88); border: 1px solid var(--border2); border-radius: 2px;
      font: bold 7px/1 system-ui, sans-serif; padding: 1px 3px;
      white-space: nowrap; box-shadow: none; pointer-events: none;
    }
    body.light-theme .node-label { background: rgba(255,255,255,0.82); border-color: #bbb; }
    /* default placement: centred above the dot (was Leaflet direction:'top') */
    .node-label-top { bottom: calc(100% + 4px); left: 50%; transform: translateX(-50%); }
    /* expanded stack members label to the right (was direction:'right') */
    .node-label-right { left: calc(100% + 6px); top: 50%; transform: translateY(-50%); }
    .node-label-hidden { display: none; }
    /* Type colours, shared by the marker captions and the popup title. These are tuned
       to glow on the dark basemap and manage only 1.5-3.3:1 on a pale one, so light mode
       swaps in darkened versions of the same hues, all clearing 4.5:1. Darkening the
       label box instead would have made it worse -- the text is the light element, so a
       darker box moves the two closer together. The marker dots keep the bright colours:
       they are solid shapes rather than 7px text, and they carry the type coding. */
    .node-label-rnode,     .node-type-rnode     { color: #f80; }
    .node-label-gateway,   .node-type-gateway   { color: #38bdf8; }
    .node-label-backbone,  .node-type-backbone  { color: #7bf; }
    .node-label-i2p,       .node-type-i2p       { color: #ad4; }
    .node-label-halow,     .node-type-halow     { color: #0cf; }
    .node-label-yggdrasil, .node-type-yggdrasil { color: #b45cff; }
    .node-type-unknown { color: #888; }
    body.light-theme .node-label-rnode,     body.light-theme .node-type-rnode     { color: #ad5c00; }
    body.light-theme .node-label-gateway,   body.light-theme .node-type-gateway   { color: #007ca8; }
    body.light-theme .node-label-backbone,  body.light-theme .node-type-backbone  { color: #006edd; }
    body.light-theme .node-label-i2p,       body.light-theme .node-type-i2p       { color: #597a16; }
    body.light-theme .node-label-halow,     body.light-theme .node-type-halow     { color: #007a99; }
    body.light-theme .node-label-yggdrasil, body.light-theme .node-type-yggdrasil { color: #9f2fff; }
    body.light-theme .node-type-unknown { color: #666; }

    #discovery-panel {
      display: none; width: 142px; flex-direction: column;
      background: var(--bg); color: var(--text); font: 12px/1.4 monospace;
      border: 1px solid var(--border); border-left: none; border-radius: 0 3px 3px 0;
    }
    #discovery-header {
      padding: 10px 12px; font-size: 11px; font-weight: bold;
      letter-spacing: 0.08em; text-transform: uppercase;
      color: var(--text-dim); border-bottom: 1px solid var(--border);
      display: flex; align-items: center; justify-content: space-between;
    }
    #discovery-list { padding: 8px 10px; display: flex; flex-direction: column; gap: 8px; }
    .disc-item { display: flex; align-items: center; gap: 7px; cursor: pointer; user-select: none; }
    .disc-item input[type=checkbox] { margin: 0; width: 13px; height: 13px; cursor: pointer; flex-shrink: 0; }
    .disc-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
    .disc-label { font-size: 11px; color: var(--text-dim); }
    #check-rnode    { accent-color: #f80; }
    #check-backbone { accent-color: #48f; }
    #check-i2p      { accent-color: #ad4; }
    #check-halow    { accent-color: #0cf; }
    #check-ygg      { accent-color: #b45cff; }
    #map-controls {
      position: fixed; bottom: 16px; left: 16px; z-index: 600;
      display: flex; gap: 6px; align-items: stretch;   /* the selects take the icon button's height */
    }
    #theme-toggle {
      background: var(--bg2); border: 1px solid var(--border2);
      color: var(--text-dim); cursor: pointer; font: 16px monospace;
      padding: 2px 6px; border-radius: 4px; line-height: 1;
    }
    #theme-toggle:hover { background: var(--bg3); color: var(--text); }
    .overlay-btn {
      background: var(--bg2); border: 1px solid var(--border2);
      color: var(--text-dim); cursor: pointer; font: 11px monospace;
      padding: 4px 8px; border-radius: 4px; line-height: 1;
    }
    #heatmap-toggle { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; }
    .overlay-btn:hover { background: var(--bg3); color: var(--text); }
    .overlay-btn.active { background: var(--accent-ghost); border-color: var(--accent-dim); color: var(--accent); }
    #heatmap-control {
      position: static; z-index: 600;
      display: flex; gap: 6px; align-items: stretch;   /* stretch: the icon buttons are taller than text-only ones */
      /* The row grew with the community buttons; on a phone it ran off the left edge. */
      flex-wrap: wrap; justify-content: flex-end; max-width: 100%;
    }
    @media (max-width: 720px) {
      #map-topbar { min-height: 82px; align-content: center; flex-wrap: wrap; }
      #map { top: 82px; }
      #panel, #nodes-panel { top: 82px; }
      #heatmap-control { justify-content: flex-start; }
    }
    @media (max-width: 440px) {
      #map-topbar { padding: 6px; gap: 4px; }
      .map-topbar-group { gap: 4px; }
      .topbar-tab { min-height: 30px; padding: 5px 7px; }
      #heatmap-control .overlay-btn { padding: 4px 6px; }
    }
    /* Community overlay: sharing dialog. Colours come from the theme variables above. */
    #pub-dialog {
      display: none; position: fixed; inset: 0; z-index: 1300;
      background: rgba(0,0,0,0.55); align-items: center; justify-content: center;
    }
    #pub-box {
      width: min(440px, calc(100vw - 24px)); max-height: calc(100vh - 24px); overflow-y: auto;
      background: var(--bg); color: var(--text); font: 12px/1.45 monospace;
      border: 1px solid var(--border2); border-radius: 6px; padding: 12px 14px;
    }
    #pub-head {
      display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;
      font-weight: bold; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-dim);
    }
    #pub-box .pub-note { color: var(--text-dim); margin: 0 0 10px; }
    #pub-box label, #pub-box .pub-or { display: block; margin: 8px 0 3px; color: var(--text-dim); }
    #pub-box input[type=text], #pub-box input[type=password] {
      width: 100%; box-sizing: border-box; background: var(--bg2); color: var(--text);
      border: 1px solid var(--border2); border-radius: 4px; padding: 5px 6px; font: 12px monospace;
    }
    /* The eye sits inside the token field, so the input needs room for it. */
    #pub-box .pub-secret { position: relative; }
    #pub-box .pub-secret input { padding-right: 30px; }
    #pub-eye {
      position: absolute; right: 2px; top: 50%; transform: translateY(-50%);
      background: none; border: 0; color: var(--text-dim); cursor: pointer; padding: 4px 6px; line-height: 0;
    }
    #pub-eye:hover, #pub-eye:focus-visible { color: var(--text); }
    #pub-eye .eye-slash { display: none; }
    #pub-eye.on .eye-slash { display: inline; }
    #pub-files { margin: 10px 0 0; color: var(--text); word-break: break-all; }
    #pub-result { margin: 10px 0 0; min-height: 1.4em; word-break: break-word; }
    #pub-result .pub-bad { color: #e57373; }
    #pub-result .pub-good { color: var(--accent); }
    #pub-result code { user-select: all; background: var(--bg2); padding: 1px 4px; border-radius: 3px; }
    .pub-actions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 12px; }
    .pub-actions .overlay-btn:disabled { opacity: 0.45; cursor: default; }
    /* Shown only standalone: when embedded, the parent site owns the language and
       has its own RU/EN switch, so a second one here would be a competing control. */
    #lang-toggle { display: none; font-weight: bold; letter-spacing: 0.5px; align-items: center; }
    #tileset-select, #size-select {
      background: var(--bg2); border: 1px solid var(--border2); color: var(--text-dim);
      font: 11px monospace; padding: 2px 4px; border-radius: 4px; cursor: pointer;
    }
    #tileset-select:hover, #size-select:hover { color: var(--text); }
    #discovery-warning {
      display: none;
      position: fixed; top: 0; left: 0; right: 0; z-index: 1200;
      background: #7a1f1f; color: #ffe; border-bottom: 1px solid #b33;
      font: 12px monospace; text-align: center; padding: 7px 36px 7px 12px;
      box-shadow: 0 2px 8px rgba(0,0,0,.4);
    }
  </style>
</head>
<body>
<div id="discovery-warning"><svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4"/><path d="M10.363 3.591l-8.106 13.534a1.914 1.914 0 0 0 1.636 2.871h16.214a1.914 1.914 0 0 0 1.636 -2.87l-8.106 -13.536a1.914 1.914 0 0 0 -3.274 0"/><path d="M12 16h.01"/></svg> <span data-i18n="warn.discovery">Discovery data unavailable — node list may be incomplete or out of date (rnstatus not available).</span></div>
<div id="hover-tip">
  <div id="hover-bars" class="hover-row">
    <div class="vbar-col">
      <span id="hover-rssi-val"></span>
      <span class="vbar-lbl">dBm</span>
    </div>
    <div class="vbar-wrap"><div class="vbar-mask" id="hover-rssi-mask"></div></div>
    <div class="vbar-wrap"><div class="vbar-mask" id="hover-snr-mask"></div></div>
    <div class="vbar-col">
      <span id="hover-snr-val"></span>
      <span class="vbar-lbl">dB</span>
    </div>
  </div>
  <div id="hover-bars-dl" class="hover-row">
    <div class="vbar-col">
      <span id="hover-dl-rssi-val"></span>
      <span class="vbar-lbl">dBm</span>
    </div>
    <div class="vbar-wrap"><div class="vbar-mask" id="hover-dl-rssi-mask"></div></div>
    <div class="vbar-wrap"><div class="vbar-mask" id="hover-dl-snr-mask"></div></div>
    <div class="vbar-col">
      <span id="hover-dl-snr-val"></span>
      <span class="vbar-lbl">dB</span>
    </div>
  </div>
</div>

<div id="nodes-panel">
  <div id="nodes-header"><span><svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 15a4 4 0 1 0 8 0a4 4 0 1 0 -8 0"/><path d="M18.5 18.5l2.5 2.5"/><path d="M4 6h16"/><path d="M4 12h4"/><path d="M4 18h4"/></svg> <span data-i18n="nodes.title">Node List</span></span><button class="panel-btn" id="nodes-close"><svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5a2 2 0 0 1 2 -2h14a2 2 0 0 1 2 2v14a2 2 0 0 1 -2 2h-14a2 2 0 0 1 -2 -2v-14"/><path d="M9 9l6 6m0 -6l-6 6"/></svg></button></div>
  <div id="nodes-search-bar"><input type="text" id="nodes-search" data-i18n-placeholder="nodes.search" placeholder="Search by name…"></div>
  <div id="nodes-geo-bar">
    <select id="geo-country" class="geo-select"><option value="">All Countries</option></select>
    <select id="geo-state"   class="geo-select"><option value="">All Regions</option></select>
    <select id="geo-city"    class="geo-select"><option value="">All Cities</option></select>
  </div>
  <div id="nodes-list"></div>
  <div id="nodes-status">
    <span data-i18n="nodes.total">Total nodes:</span><span id="nodes-total">0</span>
  </div>
</div>
<div id="discovery-container">
  <div id="discovery-open-wrap"><button id="discovery-open" data-i18n-title="title.discovery" title="Discovery"><svg class="ico" aria-hidden="true" fill="none" stroke-width="1.5" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M12 21a9.004 9.004 0 0 0 8.716-6.747M12 21a9.004 9.004 0 0 1-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 0 1 7.843 4.582M12 3a8.997 8.997 0 0 0-7.843 4.582m15.686 0A11.953 11.953 0 0 1 12 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0 1 21 12c0 .778-.099 1.533-.284 2.253m0 0A17.919 17.919 0 0 1 12 16.5c-3.162 0-6.133-.815-8.716-2.247m0 0A9.015 9.015 0 0 1 3 12c0-1.605.42-3.113 1.157-4.418" stroke-linecap="round" stroke-linejoin="round"/></svg> <span data-i18n="disc.title">Discovery</span><span id="tab-dot-rnode" class="tab-dot" style="background:#f80"></span><span id="tab-dot-backbone" class="tab-dot" style="background:#48f"></span><span id="tab-dot-i2p" class="tab-dot" style="background:#ad4"></span><span id="tab-dot-halow" class="tab-dot" style="background:#0cf"></span><span id="tab-dot-ygg" class="tab-dot" style="background:#b45cff"></span></button></div>
  <div id="discovery-panel">
    <div id="discovery-header"><span><svg class="ico" aria-hidden="true" fill="none" stroke-width="1.5" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M12 21a9.004 9.004 0 0 0 8.716-6.747M12 21a9.004 9.004 0 0 1-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 0 1 7.843 4.582M12 3a8.997 8.997 0 0 0-7.843 4.582m15.686 0A11.953 11.953 0 0 1 12 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0 1 21 12c0 .778-.099 1.533-.284 2.253m0 0A17.919 17.919 0 0 1 12 16.5c-3.162 0-6.133-.815-8.716-2.247m0 0A9.015 9.015 0 0 1 3 12c0-1.605.42-3.113 1.157-4.418" stroke-linecap="round" stroke-linejoin="round"/></svg> <span data-i18n="disc.title">Discovery</span></span><button class="panel-btn" id="discovery-close"><svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5a2 2 0 0 1 2 -2h14a2 2 0 0 1 2 2v14a2 2 0 0 1 -2 2h-14a2 2 0 0 1 -2 -2v-14"/><path d="M9 9l6 6m0 -6l-6 6"/></svg></button></div>
    <div id="discovery-list">
      <label class="disc-item">
        <input type="checkbox" id="check-rnode" checked>
        <span class="disc-dot" style="background:#f80"></span>
        <span class="disc-label">RNode</span>
      </label>
      <label class="disc-item">
        <input type="checkbox" id="check-backbone">
        <span class="disc-dot" style="background:#48f"></span>
        <span class="disc-label">Backbone</span>
      </label>
      <label class="disc-item">
        <input type="checkbox" id="check-ygg">
        <span class="disc-dot" style="background:#b45cff"></span>
        <span class="disc-label">Yggdrasil</span>
      </label>
      <label class="disc-item">
        <input type="checkbox" id="check-i2p">
        <span class="disc-dot" style="background:#ad4"></span>
        <span class="disc-label">I2P</span>
      </label>
      <label class="disc-item">
        <input type="checkbox" id="check-halow">
        <span class="disc-dot" style="background:#0cf"></span>
        <span class="disc-label">HaLow</span>
      </label>
    </div>
    <div id="discovery-options">
      <label class="disc-item">
        <input type="checkbox" id="check-labels" checked>
        <span class="disc-label" data-i18n="disc.labels">Labels</span>
      </label>
    </div>
  </div>
</div>
<div id="map"></div>
<div id="map-topbar"><div class="map-topbar-group"><div id="nodes-open-wrap"><button id="nodes-open" class="topbar-tab" data-i18n-title="title.nodes" title="Node List"><svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 15a4 4 0 1 0 8 0a4 4 0 1 0 -8 0"/><path d="M18.5 18.5l2.5 2.5"/><path d="M4 6h16"/><path d="M4 12h4"/><path d="M4 18h4"/></svg> <span data-i18n="nodes.title">Node List</span></button></div>
<div id="panel-open-wrap"><button id="panel-open" class="topbar-tab" data-i18n-title="title.stream" title="Signal Report Stream"><svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 18l0 -3"/><path d="M10 18l0 -6"/><path d="M14 18l0 -9"/><path d="M18 18l0 -12"/></svg> <span data-i18n="stream.title">Signal Report Stream</span></button></div></div>
<div id="heatmap-control">
  <button id="file-close" class="overlay-btn" style="display:none" ><svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5a2 2 0 0 1 2 -2h14a2 2 0 0 1 2 2v14a2 2 0 0 1 -2 2h-14a2 2 0 0 1 -2 -2v-14"/><path d="M9 9l6 6m0 -6l-6 6"/></svg> <span data-i18n="file.close">Close file</span></button>
  <button id="file-open" class="overlay-btn" data-i18n-title="file.hint" title="Open a GeoJSON, CSV or GPX file exported by the wardrive app"><svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 19l2.757 -7.351a1 1 0 0 1 .936 -.649h12.307a1 1 0 0 1 .986 1.164l-.996 5.211a2 2 0 0 1 -1.964 1.625h-14.026a2 2 0 0 1 -2 -2v-11a2 2 0 0 1 2 -2h4l3 3h7a2 2 0 0 1 2 2v2"/></svg> <span data-i18n="file.open">Open file</span></button>
  <input type="file" id="file-input" accept=".geojson,.json,.csv,.gpx" multiple style="display:none">
{% if overlay_on %}
  <button id="file-publish" class="overlay-btn" data-i18n-title="pub.hint" title="Share the opened file on the community overlay"><svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2 -2v-2"/><path d="M7 9l5 -5l5 5"/><path d="M12 4l0 12"/></svg> <span data-i18n="pub.button">Share</span></button>
{% endif %}
  <button id="heatmap-toggle" class="overlay-btn"><svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 18l0 -3"/><path d="M10 18l0 -6"/><path d="M14 18l0 -9"/><path d="M18 18l0 -12"/></svg> Signal Overlay</button>
  <button id="lang-toggle" class="overlay-btn" data-i18n-title="title.lang" title="Language">EN</button>
</div>
</div>
{% if overlay_on %}
<div id="pub-dialog" role="dialog" aria-modal="true" aria-labelledby="pub-title">
  <div id="pub-box">
    <div id="pub-head"><span id="pub-title" data-i18n="pub.title">Share on the community overlay</span><button class="panel-btn" id="pub-close" aria-label="Close"><svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6l-12 12"/><path d="M6 6l12 12"/></svg></button></div>
    <p class="pub-note" data-i18n="pub.note">The file is checked by the server and, if it passes, its positions (rounded to about 1 m), signal readings and the hour of each reading become public. Your name and token are never shown.</p>
    <div id="pub-files"></div>
    <label for="pub-token" data-i18n="pub.token">Access token</label>
    <div class="pub-secret">
      <input type="password" id="pub-token" autocomplete="off" spellcheck="false">
      <button type="button" id="pub-eye" aria-pressed="false" aria-label="Show token" data-i18n-title="pub.show" title="Show token"><svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 12a2 2 0 1 0 4 0a2 2 0 0 0 -4 0"/><path d="M21 12c-2.4 4 -5.4 6 -9 6c-3.6 0 -6.6 -2 -9 -6c2.4 -4 5.4 -6 9 -6c3.6 0 6.6 2 9 6"/><path class="eye-slash" d="M3 3l18 18"/></svg></button>
    </div>
    <div class="pub-or" data-i18n="pub.or">No token yet? Redeem an invite from a contributor:</div>
    <input type="text" id="pub-code" autocomplete="off" spellcheck="false" placeholder="inv_..." aria-label="Invite code">
    <input type="text" id="pub-name" maxlength="40" autocomplete="off" style="margin-top:6px" data-i18n-placeholder="pub.name" placeholder="Your name or callsign" aria-label="Name">
    <div id="pub-result" role="status" aria-live="polite"></div>
    <div class="pub-actions">
      <button id="pub-send" class="overlay-btn active" data-i18n="pub.send">Publish</button>
      <button id="pub-redeem" class="overlay-btn" data-i18n="pub.redeem">Redeem invite</button>
      <button id="pub-invite" class="overlay-btn" data-i18n="pub.invite">Create invite</button>
      <button id="pub-forget" class="overlay-btn" data-i18n="pub.forget">Forget token</button>
    </div>
  </div>
</div>
{% endif %}
<div id="map-controls">
  <button id="theme-toggle" data-i18n-title="title.theme" title="Light / Dark Mode"><svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.173 14.83a4 4 0 1 1 5.657 -5.657"/><path d="M11.294 12.707l.174 .247a7.5 7.5 0 0 0 8.845 2.492a9 9 0 0 1 -14.671 2.914"/><path d="M3 12h1"/><path d="M12 3v1"/><path d="M5.6 5.6l.7 .7"/><path d="M3 21l18 -18"/></svg></button>
  <select id="size-select" data-i18n-title="title.size" title="Marker Size">
    <option value="small" data-i18n="size.small">Small</option>
    <option value="medium" data-i18n="size.medium">Medium</option>
    <option value="large" data-i18n="size.large">Large</option>
    <option value="xl" data-i18n="size.xl">XL</option>
  </select>
  <select id="tileset-select" data-i18n-title="title.tileset" title="Map Tileset"></select>
</div>

<div id="panel">
  <div id="panel-header"><span id="panel-title"><svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 18l0 -3"/><path d="M10 18l0 -6"/><path d="M14 18l0 -9"/><path d="M18 18l0 -12"/></svg> Список маяков</span><button class="panel-btn" id="panel-close"><svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5a2 2 0 0 1 2 -2h14a2 2 0 0 1 2 2v14a2 2 0 0 1 -2 2h-14a2 2 0 0 1 -2 -2v-14"/><path d="M9 9l6 6m0 -6l-6 6"/></svg></button></div>
  <div id="filter-bar">
    <div id="filter-dates">
      <input type="date" id="filter-from" class="date-input">
      <span class="date-sep">—</span>
      <input type="date" id="filter-to" class="date-input">
    </div>
    <div id="filter-btns">
      <button class="panel-btn" data-preset="day" data-i18n="filter.day">Day</button>
      <button class="panel-btn" data-preset="week" data-i18n="filter.week">Week</button>
      <button class="panel-btn" data-preset="month" data-i18n="filter.month">Month</button>
      <button class="panel-btn" data-preset="year" data-i18n="filter.year">Year</button>
      <button class="panel-btn" id="filter-apply" data-i18n="filter.apply">Filter</button>
      <button class="panel-btn" id="filter-clear" data-i18n="filter.clear">Clear</button>
    </div>
  </div>
  <div id="panel-list"></div>
  <div id="status"><span id="status-time" class="ts" data-i18n="status.waiting">Waiting…</span><span id="status-total"></span></div>
</div>

<script src="{{ js_base }}/maplibre-gl.js"></script>
{% if not raster %}<script src="{{ js_base }}/pmtiles.js"></script>
{% endif %}<script>
// Where the basemap comes from, chosen by the operator in wardrive_map.json:
//   provider  'local' | 'remote' | 'osm' | 'raster'
//   base      origin of the install lending its basemap ('' unless remote)
//   raster    XYZ tile description for the raster providers, else null
const BASEMAP = {{ basemap | safe }};
const RASTER = BASEMAP.raster || null;
const ASSET_BASE = BASEMAP.base || '';

// pmtiles.js registers the pmtiles:// protocol, so MapLibre pulls the whole basemap
// out of one archive file over HTTP range requests -- nginx serves it as a static
// file and there is no tile server process. A raster basemap has no archive, and the
// page does not load pmtiles.js for it.
if (!RASTER) {
  const pmtilesProtocol = new pmtiles.Protocol();
  maplibregl.addProtocol('pmtiles', pmtilesProtocol.tile);
}

// Every style renders {name:nonlatin} beside {name:latin}, so a worldwide basemap puts
// Arabic and Hebrew place names on screen. Without this plugin MapLibre draws them
// unshaped and in logical rather than display order. Loaded lazily: it only fetches
// when RTL text is actually encountered, which for most views here is never. Self-hosted
// because it runs as a Worker and would otherwise be a cross-origin fetch. A remote
// basemap takes it from the same install as the tiles; raster tiles have their labels
// painted in and need no shaping.
if (!RASTER) {
  maplibregl.setRTLTextPlugin(ASSET_BASE + '/static/js/mapbox-gl-rtl-text.min.js', true)
    .catch(e => console.warn('RTL text plugin unavailable:', e));
}

// Leaflet renders 256px tiles, MapLibre 512px, so the same visual scale sits one zoom
// level lower here. Zoom values stay in Leaflet terms everywhere below (and in
// /etc/reticulum/wardrive_map.json, which therefore needs no edit) and convert on use.
const ZOOM_OFFSET = -1;
const toMapZoom = z => z + ZOOM_OFFSET;

// All five ride the same vector tileset -- a style is just a JSON recipe, so extra
// basemaps cost nothing but the style file. Order matches the pre-MapLibre lineup so
// a stored 'tileset' index still selects the same basemap it used to.
// `id` is the persisted identity of a basemap and must stay stable; the list order is
// presentation only. Storing the index instead meant reordering the list silently moved
// every existing visitor to whichever basemap inherited their old number.
//
// `dark` marks a basemap that wants dark page chrome. It is a property of the style
// rather than an index comparison: the selector used to infer "dark" as "index 0",
// which quietly put light buttons and tabs over every other dark basemap added since.
const VECTOR_TILESETS = [
  { id: 'green',            label: 'Green', labelKey: 'tileset.green', style: '/basemap/rns-dark.json', dark: true },
  { id: 'voyager',          label: 'Voyager',             style: '/basemap/voyager.json',          dark: false },
  { id: 'positron',         label: 'Light', labelKey: 'tileset.light', style: '/basemap/positron.json', dark: false },
  { id: 'osm-bright',       label: 'OSM',                 style: '/basemap/osm-bright.json',       dark: false },
  { id: 'dark-matter',      label: 'Dark', labelKey: 'tileset.dark', style: '/basemap/dark-matter.json', dark: true },
];

// A raster provider is one fixed picture of the world, so it is a single entry whose
// style is built here rather than fetched: one source, one layer, no glyphs or sprite.
// tileSize has to match what the server renders (256 for OSM) or MapLibre asks for a
// zoom level that makes every label half or double its intended size.
function rasterStyle(r) {
  return {
    version: 8,
    sources: { basemap: {
      type: 'raster', tiles: [].concat(r.url), tileSize: r.tile_size || 256,
      maxzoom: r.max_zoom || 19, attribution: r.attribution || '',
    } },
    layers: [{ id: 'basemap', type: 'raster', source: 'basemap' }],
  };
}

// Styles of a remote basemap are fetched from the install that owns the tiles, which
// also pins them to its current archive version.
const TILESETS = RASTER
  ? [{ id: 'raster', label: RASTER.label || 'Map', style: rasterStyle(RASTER), dark: false }]
  : VECTOR_TILESETS.map(t => Object.assign({}, t, { style: ASSET_BASE + t.style }));
const tilesetIndex = id => TILESETS.findIndex(t => t.id === id);

// The theme toggle pairs page chrome with a basemap: dark chrome gets the site-themed
// green basemap, light chrome gets Positron. Looked up by id so reordering the list
// cannot repoint them.
const DARK_TILESET  = tilesetIndex('green');
const LIGHT_TILESET = tilesetIndex('positron');
// False for a raster provider, which has one basemap and so nothing to pair. The theme
// toggle then recolours the page chrome only.
const THEME_PAIRED  = DARK_TILESET >= 0 && LIGHT_TILESET >= 0;
// What a visitor with no usable stored choice gets.
const DEFAULT_TILESET = Math.max(0, DARK_TILESET);

// Order of the list back when the index was what got persisted. A stored number is read
// through this once and rewritten as an id, so an existing visitor keeps the basemap
// they actually chose instead of being shuffled by the reorder.
const _LEGACY_ORDER = ['dark-matter', 'voyager', 'voyager-nolabels', 'positron', 'osm-bright', 'green'];

// Basemaps that no longer exist, mapped to their closest survivor. voyager-nolabels was
// retired once the label toggle made it redundant; anyone still holding it -- by id, or
// by the legacy index that resolves to it -- lands on Positron rather than being bounced
// to the default.
const _RETIRED = { 'voyager-nolabels': 'positron' };

function _restoreTileset() {
  const raw = localStorage.getItem('tileset');
  if (raw === null) return DEFAULT_TILESET;
  let i = tilesetIndex(_RETIRED[raw] || raw);
  if (i >= 0) return i;
  if (/^[0-9]+$/.test(raw)) {                       // legacy numeric index
    const was = _LEGACY_ORDER[parseInt(raw, 10)];
    i = tilesetIndex(_RETIRED[was] || was);
    if (i >= 0) { localStorage.setItem('tileset', TILESETS[i].id); return i; }
  }
  return DEFAULT_TILESET;
}

// Resolved before the map is built so the stored choice loads directly, rather than
// rendering one basemap and immediately swapping it out.
let currentTileset = _restoreTileset();

const map = new maplibregl.Map({
  container:         'map',
  style:             TILESETS[currentTileset].style,
  center:            [{{ init_lon }}, {{ init_lat }}],
  zoom:              toMapZoom({{ init_zoom }}),
  maxZoom:           toMapZoom(19),
  attributionControl: { compact: true },
  dragRotate:        false,
  pitchWithRotate:   false,
});
const mapAttributionRoot = document.getElementById('map');
function openAttributionLinksExternally() {
  mapAttributionRoot.querySelectorAll('.maplibregl-ctrl-attrib a').forEach(link => {
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
  });
}
new MutationObserver(openAttributionLinksExternally).observe(mapAttributionRoot, { childList: true, subtree: true });
openAttributionLinksExternally();
const GATEWAY_POSITION = {{ gateway_position | safe }};
const HOME_POSITION = GATEWAY_POSITION || { lat: {{ init_lat }}, lon: {{ init_lon }} };
function homeZoom() {
  if (!GATEWAY_POSITION) return toMapZoom({{ init_zoom }});
  const width = Math.max(1, map.getContainer().clientWidth);
  const mppAtZoom0 = 156543.03392 * Math.cos(GATEWAY_POSITION.lat * Math.PI / 180);
  return Math.max(0, Math.min(19, Math.log2((mppAtZoom0 * width) / 10000)));
}
// Keep the gateway in the centre and show roughly 10 km across the viewport.
// The previous auto-fit used every discovery node, which could zoom out to the world.
map.once('load', () => map.jumpTo({ center: [HOME_POSITION.lon, HOME_POSITION.lat], zoom: homeZoom() }));
// Keep the flat north-up view the Leaflet map had. Dropping these three lines is all
// that is needed to enable rotation/pitch.
map.touchZoomRotate.disableRotation();

// leaflet.heat blurred points onto a 2D canvas; MapLibre's heatmap layer is a GPU
// kernel, so radius/intensity here approximate the old radius:20 / blur:15 /
// minOpacity:0.4 / maxZoom:17 rather than reproducing them exactly. Tune here.
const HEAT_RADIUS    = 20;
const HEAT_INTENSITY = ['interpolate', ['linear'], ['zoom'], 0, 0.6, toMapZoom(17), 1.4];
// The 0.05 stop stands in for leaflet.heat's minOpacity floor, which kept sparse
// points visible instead of fading them to nothing.
// Red = weak / sparse, green = strong / dense — same logic as the RSSI gauges.
const HEAT_UL_COLOR = ['interpolate', ['linear'], ['heatmap-density'],
  0,    'rgba(239,68,68,0)',
  0.05, 'rgba(239,68,68,0.55)',
  0.35, '#f97316',
  0.55, '#eab308',
  0.75, '#84cc16',
  1,    '#16a34a'];
const HEAT_DL_COLOR = ['interpolate', ['linear'], ['heatmap-density'],
  0,    'rgba(220,38,38,0)',
  0.05, 'rgba(220,38,38,0.55)',
  0.35, '#ea580c',
  0.6,  '#ca8a04',
  0.85, '#65a30d',
  1,    '#15803d'];

function heatPaint(color) {
  return {
    'heatmap-radius':    HEAT_RADIUS,
    'heatmap-intensity': HEAT_INTENSITY,
    'heatmap-weight':    ['get', 'i'],
    'heatmap-color':     color,
  };
}

// Point data can arrive before the style finishes loading, so it is buffered in these
// arrays and pushed to the sources by syncHeat() once the style is ready.
let heatUlFeatures = [];
let heatDlFeatures = [];
let fileMissFeatures = [];
// True while an exported file is shown instead of the live data; declared this early
// because fetchDlOverlay() runs as soon as it is defined.
let fileMode       = false;
let styleReady     = false;
// Community overlay (see the "Community overlay" block further down). Declared here
// because syncHeat() and applyOverlayMode() read them as soon as the style loads.
const COMMUNITY    = {{ 'true' if overlay_on else 'false' }};
// Community reports are drawn together with the Signal Overlay whenever sharing is enabled; there is no
// separate button for them.
const commOn       = COMMUNITY;
let commUl         = [];
let commDl         = [];
let commPoints     = [];   // the community reports with their readings, for the hover tooltip
const filePoints   = [];   // the answered rows of the opened file(s), likewise
let openedFiles    = [];   // the File objects behind the current file view, uploaded as-is when sharing

function heatFeature(lat, lon, i) {
  return { type: 'Feature', properties: { i }, geometry: { type: 'Point', coordinates: [lon, lat] } };
}
function emptyFC() { return { type: 'FeatureCollection', features: [] }; }

function syncHeat() {
  if (!styleReady) return;
  map.getSource('heat-ul').setData({ type: 'FeatureCollection', features: heatUlFeatures });
  map.getSource('heat-dl').setData({ type: 'FeatureCollection', features: heatDlFeatures });
  map.getSource('file-miss').setData({ type: 'FeatureCollection', features: fileMissFeatures });
  if (COMMUNITY) {
    map.getSource('comm-ul').setData({ type: 'FeatureCollection', features: commUl });
    map.getSource('comm-dl').setData({ type: 'FeatureCollection', features: commDl });
  }
}

// 'style.load' rather than 'load': it fires for the initial style *and* after every
// setStyle(), and setStyle() drops all sources/layers. Node markers are DOM and survive
// it untouched, so only the heat layers need rebuilding.
map.on('style.load', () => {
  // Wrapped because MapLibre's fire() has no try/catch: a throw here would skip every
  // line below it in this callback. That is how the label state fell out of sync on a
  // basemap switch -- setStyle tears the sources down, and a failed addLayer took
  // applyGeoLabels and renderDiscovery down with it, silently.
  try {
    if (!map.getSource('heat-ul')) {
      map.addSource('heat-ul', { type: 'geojson', data: emptyFC() });
      map.addSource('heat-dl', { type: 'geojson', data: emptyFC() });
      map.addSource('file-miss', { type: 'geojson', data: emptyFC() });
      if (COMMUNITY) {
        map.addSource('comm-ul', { type: 'geojson', data: emptyFC() });
        map.addSource('comm-dl', { type: 'geojson', data: emptyFC() });
      }
    }
    if (!map.getLayer('heat-ul')) {
      map.addLayer({ id: 'heat-ul', type: 'heatmap', source: 'heat-ul', paint: heatPaint(HEAT_UL_COLOR) });
    }
    // Discrete dots on top of the heat blob so a single sample (or one colocated
    // with the gateway marker) stays visible even when heatmap density is thin.
    if (!map.getLayer('heat-ul-dots')) {
      map.addLayer({ id: 'heat-ul-dots', type: 'circle', source: 'heat-ul', paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 11, 5, 16, 9],
        'circle-color': ['interpolate', ['linear'], ['get', 'i'],
          0.15, '#ef4444', 0.35, '#f97316', 0.6, '#eab308', 0.8, '#84cc16', 1, '#16a34a'],
        'circle-opacity': 0.95,
        'circle-stroke-width': 1.5,
        'circle-stroke-color': '#ffffff' } });
    }
    if (!map.getLayer('heat-dl')) {
      map.addLayer({ id: 'heat-dl', type: 'heatmap', source: 'heat-dl', paint: heatPaint(HEAT_DL_COLOR) });
    }
    if (!map.getLayer('heat-dl-dots')) {
      map.addLayer({ id: 'heat-dl-dots', type: 'circle', source: 'heat-dl', paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 11, 4, 16, 8],
        'circle-color': ['interpolate', ['linear'], ['get', 'i'],
          0.15, '#dc2626', 0.35, '#ea580c', 0.6, '#ca8a04', 0.85, '#65a30d', 1, '#15803d'],
        'circle-opacity': 0.9,
        'circle-stroke-width': 1.2,
        'circle-stroke-color': '#ffffff' } });
    }
    if (COMMUNITY && !map.getLayer('comm-ul')) {
      // Reports shared by other people. Same colour ramps as your own data; which of the
      // two directions shows follows the Signal Overlay button, and they appear and
      // disappear with it.
      map.addLayer({ id: 'comm-ul', type: 'heatmap', source: 'comm-ul', paint: heatPaint(HEAT_UL_COLOR) });
      map.addLayer({ id: 'comm-dl', type: 'heatmap', source: 'comm-dl', paint: heatPaint(HEAT_DL_COLOR) });
    }
    if (!map.getLayer('file-miss')) {
      // Probes nobody answered, from an opened file: the coverage holes.
      map.addLayer({ id: 'file-miss', type: 'circle', source: 'file-miss', paint: {
        'circle-radius': 3, 'circle-color': '#e53935', 'circle-opacity': 0.75,
        'circle-stroke-width': 0.5, 'circle-stroke-color': '#fff' } });
    }
    setupRulerLayers();
    styleReady = true;
    syncHeat();
    applyOverlayMode();
    if (COMMUNITY && commOn) scheduleCommunityFetch(0);   // setStyle() emptied the source
  } catch (e) {
    console.warn('heat layer setup failed after style load:', e);
  }
  applyGeoLabels();
  applyLabelLanguage();
  renderDiscovery();
});

// Declared ahead of everything that renders text. t() is a hoisted function but
// uiLang is a let, so any call before this point lands in the temporal dead zone and
// throws -- which killed the whole script, not just the string being translated.
// ---- UI language ----
// The RU/EN switch updates the map UI. Node types and protocol names remain proper
// nouns; ordinary interface labels are translated.
const I18N = {
  en: {},   // English is the source text; an empty dict means "use what is in the DOM"
  ru: {
    'warn.discovery':  'Данные обнаружения недоступны — список узлов может быть неполным или устаревшим (rnstatus недоступен).',
    'nodes.title':     'Список узлов',
    'title.nodes':     'Список узлов',
    'nodes.search':    'Поиск по имени…',
    'nodes.total':     'Всего узлов:',
    'nodes.empty':     'Нет узлов',
    'stream.from':     'от',
    'geo.allCountries':'Все страны',
    'geo.allRegions':  'Все регионы',
    'geo.allCities':   'Все города',
    'geo.geocoding':   'Геокодирование…',
    'disc.labels':     'Подписи',
    'disc.title':      'Обнаружение',
    'title.discovery': 'Обнаружение',
    'size.small':      'Мелкие',
    'size.medium':     'Средние',
    'size.large':      'Крупные',
    'size.xl':         'Очень крупные',
    'title.theme':     'Светлая / тёмная тема',
    'title.size':      'Размер маркеров',
    'title.tileset':   'Подложка карты',
    'tileset.green':   'Зелёная',
    'tileset.light':   'Светлая',
    'tileset.dark':    'Тёмная',
    'title.lang':      'Язык',
    'title.home':      'Вернуться к домашней точке шлюза',
    'title.hideLabels':'Скрыть подписи на карте',
    'title.showLabels':'Показать подписи на карте',
    'title.rulerOn':   'Линейка включена — нажмите, чтобы выключить',
    'title.rulerOff':  'Измерить расстояние между двумя точками (Ctrl+клик по карте — быстрая линейка)',
    'ruler.first':     'Линейка: выберите первую точку',
    'ruler.second':    'Линейка: выберите вторую точку',
    'ruler.distance':  'Расстояние:',
    'ruler.reset':     'Сброс',
    'ruler.m':         'м',
    'ruler.km':        'км',
    'ruler.bearing':   'Азимут:',
    'ruler.true':      'И',
    'ruler.mag':       'М',
    'dir.N':           'С',
    'dir.NE':          'СВ',
    'dir.E':           'В',
    'dir.SE':          'ЮВ',
    'dir.S':           'Ю',
    'dir.SW':          'ЮЗ',
    'dir.W':           'З',
    'dir.NW':          'СЗ',
    'title.stream':    'Список маяков',
    'stream.title':    'Список маяков',
    'stream.filtered': 'Список маяков (фильтр)',
    'overlay.off':     'Наложение сигнала',
    'overlay.ul':      'Восходящий',
    'overlay.dl':      'Нисходящий',
    'overlay.uldl':    'Оба направления',
    'filter.day':      'День',
    'filter.week':     'Неделя',
    'filter.month':    'Месяц',
    'filter.year':     'Год',
    'filter.apply':    'Фильтр',
    'filter.clear':    'Сброс',
    'status.waiting':  'Ожидание…',
    'status.total':    'Всего: ',
    'status.points':   ' точек',
    'status.point':    ' точка',
    'status.filtered': 'Отфильтровано до ',
    'status.last':     'Последний анонс в ',
    'status.dbdown':   'БД недоступна — повтор…',
    'file.open':       'Открыть файл',
    'file.close':      'Закрыть файл',
    'file.hint':       'Открыть файл GeoJSON, CSV или GPX, экспортированный из приложения wardrive',
    'file.unreadable': 'Не удалось прочитать файл: ',
    'file.skipped':    ' строк пропущено',
    'file.holes':      ' без ответа',
    'pub.button':      'Поделиться',
    'pub.hint':        'Поделиться открытым файлом на общей карте сообщества',
    'pub.title':       'Поделиться на общей карте',
    'pub.note':        'Сервер проверит файл и, если он пройдёт проверку, позиции (с округлением до ~1 м), уровни сигнала и час каждого замера станут общедоступными. Ваше имя и токен не показываются.',
    'pub.token':       'Токен доступа',
    'pub.show':        'Показать токен',
    'pub.hide':        'Скрыть токен',
    'pub.or':          'Нет токена? Активируйте приглашение от участника:',
    'pub.name':        'Ваше имя или позывной',
    'pub.send':        'Опубликовать',
    'pub.redeem':      'Активировать приглашение',
    'pub.invite':      'Создать приглашение',
    'pub.forget':      'Забыть токен',
    'pub.nofile':      'Сначала откройте экспортированный файл, затем поделитесь им отсюда.',
    'pub.needToken':   'Сначала введите токен доступа.',
    'pub.forgotten':   'Токен удалён из этого браузера.',
    'pub.welcome':     'Приглашение принято. Сохраните токен, он показывается только один раз: ',
    'pub.invited':     'Одноразовый код приглашения, действует две недели: ',
    'pub.published':   ' опубликовано',
    'pub.dupes':       ' уже были',
    'pub.skipped':     'пропущено: ',
    'pub.err.unauthorized':   'Токен недействителен или учётная запись отключена.',
    'pub.err.forbidden':      'Этой учётной записи это действие недоступно.',
    'pub.err.duplicate_file': 'Вы уже публиковали именно этот файл.',
    'pub.err.all_duplicates': 'Всё из этого файла уже опубликовано.',
    'pub.err.quota_batches':  'Достигнут суточный лимит загрузок. Попробуйте завтра.',
    'pub.err.quota_rows':     'Достигнут суточный лимит замеров. Попробуйте завтра.',
    'pub.err.invite_quota':   'Все ваши приглашения использованы.',
    'pub.err.overlay_full':   'Общий слой сейчас переполнен.',
    'pub.err.too_large':      'Файл слишком большой.',
    'pub.err.empty':          'Файл пуст.',
    'pub.err.bad_invite':     'Код приглашения недействителен, уже использован или просрочен.',
    'pub.err.bad_name':       'Введите имя или позывной.',
    'pub.err.bad_request':    'Введите код приглашения и имя.',
    'pub.err.no_valid_rows':  'В файле нет замеров, прошедших проверку.',
    'pub.err.too_many_invalid': 'Слишком много замеров в файле не прошли проверку.',
    'pub.err.span_too_long':  'Замеры охватывают слишком долгий период для одной загрузки.',
    'pub.err.synthetic':      'Замеры похожи на сгенерированные.',
    'pub.err.too_many_rows':  'В файле слишком много строк.',
    'pub.err.not_utf8':       'Файл не является текстом.',
    'pub.err.bad_json':       'Файл не является корректным JSON.',
    'pub.err.bad_csv':        'Файл не является корректным CSV.',
    'pub.err.bad_xml':        'Файл не является корректным XML.',
    'pub.err.xml_dtd_forbidden': 'В файле есть DTD, это не допускается.',
    'pub.err.bad_geojson':    'Файл не является экспортом GeoJSON.',
    'pub.err.rate_limited':   'Слишком много запросов. Подождите минуту.',
    'pub.err.network':        'Не удалось связаться с сервером.',
    'src.file':        'Файл',
    'src.ann':         'Анонс',
    'src.pkt':         'Пакет',
    'src.relay':       'Ретранслятор',
    'misc.you':        'Вы',
    'misc.copied':     'Скопировано!',
    'misc.unknown':    'неизвестно',
    'age.s':           'с назад',
    'age.m':           'мин назад',
    'age.h':           'ч назад',
    'age.d':           'дн назад',
    'pop.heard':       'слышен',
    'pop.gateway':     'Шлюз',
    'pop.freq':        'частота',
    'pop.bw':          'полоса',
    'pop.alt':         'высота',
    'pop.remote':      'адрес',
    'pop.hash':        'хеш',
    'pop.fw':          'прошивка',
    'pop.up':          'аптайм',
    'pop.ver':         'версия',
    'pop.tx':          'отпр',
    'pop.rx':          'получ',
    'pop.pkts':        'пак',
  },
};
const DEFAULT_PAGE_TITLE = document.title;

const EMBEDDED = window.self !== window.top;

function _defaultUiLang() {
  const saved = localStorage.getItem('lang');
  if (saved === 'ru' || saved === 'en') return saved;
  const nav = (navigator.languages && navigator.languages[0]) || navigator.language || '';
  return String(nav).toLowerCase().startsWith('ru') ? 'ru' : 'en';
}

let uiLang = _defaultUiLang();

// English falls through to the markup, so every key needs a default: the source text is
// the English translation and duplicating it here would be two places to keep in step.
const _EN_DEFAULTS = {};
function t(key, fallback) {
  if (uiLang !== 'en') {
    const v = I18N[uiLang] && I18N[uiLang][key];
    if (v !== undefined) return v;
  }
  return _EN_DEFAULTS[key] !== undefined ? _EN_DEFAULTS[key] : (fallback !== undefined ? fallback : key);
}

function applyI18n() {
  document.documentElement.lang = uiLang;
  document.title = uiLang === 'ru' ? 'Карта покрытия Wardrive' : DEFAULT_PAGE_TITLE;
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const k = el.getAttribute('data-i18n');
    if (_EN_DEFAULTS[k] === undefined) _EN_DEFAULTS[k] = el.textContent;
    el.textContent = t(k);
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    const k = el.getAttribute('data-i18n-title');
    if (_EN_DEFAULTS[k] === undefined) _EN_DEFAULTS[k] = el.getAttribute('title') || '';
    el.title = t(k);
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    const k = el.getAttribute('data-i18n-placeholder');
    if (_EN_DEFAULTS[k] === undefined) _EN_DEFAULTS[k] = el.placeholder || '';
    el.placeholder = t(k);
  });
  const homeBtn = document.getElementById('home-btn');
  if (homeBtn) {
    homeBtn.title = t('title.home', 'Return to the gateway location');
    homeBtn.setAttribute('aria-label', homeBtn.title);
  }
  const lb = document.getElementById('lang-toggle');
  if (lb) lb.textContent = uiLang.toUpperCase();
}

// Panels, popups and the status line build their text in JS, so translating the DOM is
// not enough -- they have to be rebuilt. Guarded individually because this runs before
// some of them exist on the very first call.
function refreshLocalisedViews() {
  try { applyOverlayMode(); } catch (e) {}
  try { buildGeoDropdowns(); } catch (e) {}
  try { renderNodesList(); } catch (e) {}
  try { renderDiscovery(); } catch (e) {}   // rebuilds popups with translated fields
  try { applyGeoLabels(); } catch (e) {}    // refreshes the labels button title
  try { applyRuler(); applyQuickRuler(); } catch (e) {}   // button title, readout text, units and directions
  try {
    if (fileMode) setPanelTitle(ICON_FILE, openedFiles.map(f => f.name).join(', '));
    else setPanelTitle(SIGNAL_ICON, t(filterActive ? 'stream.filtered' : 'stream.title', filterActive ? 'Beacons (filtered)' : 'Beacons'));
    panelList.innerHTML = '';
    allPoints.slice(-MAX_ENTRIES).forEach(addPanelEntry);
    if (total > 0) {
      const d = new Date(lastTs * 1000);
      const tm = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
      document.getElementById('status-time').textContent =
        (filterActive ? t('status.filtered', 'Filtered to ') : t('status.last', 'Last announce at ')) + tm;
      document.getElementById('status-total').textContent = t('status.total', 'Total: ') + total +
        (uiLang === 'ru' ? t('status.points', ' points') : ' point' + (total !== 1 ? 's' : ''));
    }
  } catch (e) {}
}

// Language drives the map labels too, so one control moves both: ru selects the
// Cyrillic name, en the Latin one.
function setUiLang(lang, propagate) {
  if (lang !== 'ru' && lang !== 'en') return;
  uiLang = lang;
  localStorage.setItem('lang', lang);
  applyI18n();
  setLabelLang(lang);
  if (propagate) refreshLocalisedViews();
}

const OVERLAY_MODES = [
  { key: 'overlay.off',  en: 'Signal Overlay',      ul: false, dl: false },
  { key: 'overlay.ul',   en: 'Uplink',               ul: true,  dl: false },
  { key: 'overlay.dl',   en: 'Downlink',             ul: false, dl: true  },
  { key: 'overlay.uldl', en: 'Both directions',      ul: true,  dl: true  },
];
let overlayModeIdx = parseInt(localStorage.getItem('overlay-mode') || '0', 10);
if (overlayModeIdx < 0 || overlayModeIdx >= OVERLAY_MODES.length) overlayModeIdx = 0;

const heatBtn = document.getElementById('heatmap-toggle');
const ICON_UP = '<svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4l0 10"/><path d="M12 4l4 4"/><path d="M12 4l-4 4"/><path d="M4 20l16 0"/></svg>';
const ICON_DOWN = '<svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20l0 -10"/><path d="M12 20l4 -4"/><path d="M12 20l-4 -4"/><path d="M4 4l16 0"/></svg>';
const ICON_BOTH = '<svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 12h-6"/><path d="M5 15l-3 -3l3 -3"/><path d="M22 12h-6"/><path d="M19 15l3 -3l-3 -3"/><path d="M12 4v16"/></svg>';
function setPanelTitle(iconHtml, text) {
  const el = document.getElementById('panel-title');
  el.innerHTML = iconHtml + ' ';
  el.appendChild(document.createTextNode(text));
}
const ICON_FILE = '<svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 19l2.757 -7.351a1 1 0 0 1 .936 -.649h12.307a1 1 0 0 1 .986 1.164l-.996 5.211a2 2 0 0 1 -1.964 1.625h-14.026a2 2 0 0 1 -2 -2v-11a2 2 0 0 1 2 -2h4l3 3h7a2 2 0 0 1 2 2v2"/></svg>';
const SIGNAL_ICON = '<svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 18l0 -3"/><path d="M10 18l0 -6"/><path d="M14 18l0 -9"/><path d="M18 18l0 -12"/></svg>';

function applyOverlayMode() {
  const m = OVERLAY_MODES[overlayModeIdx];
  heatBtn.innerHTML = (m.ul && m.dl ? ICON_BOTH : m.ul ? ICON_UP : m.dl ? ICON_DOWN : SIGNAL_ICON) + ' ';
  heatBtn.appendChild(document.createTextNode(t(m.key, m.en)));
  heatBtn.classList.toggle('active', m.ul || m.dl);
  if (!styleReady) return;   // map 'load' calls this again once the layers exist
  map.setLayoutProperty('heat-ul', 'visibility', m.ul ? 'visible' : 'none');
  map.setLayoutProperty('heat-dl', 'visibility', m.dl ? 'visible' : 'none');
  if (map.getLayer('heat-ul-dots')) {
    map.setLayoutProperty('heat-ul-dots', 'visibility', m.ul ? 'visible' : 'none');
  }
  if (map.getLayer('heat-dl-dots')) {
    map.setLayoutProperty('heat-dl-dots', 'visibility', m.dl ? 'visible' : 'none');
  }
  if (COMMUNITY) {
    map.setLayoutProperty('comm-ul', 'visibility', commOn && m.ul ? 'visible' : 'none');
    map.setLayoutProperty('comm-dl', 'visibility', commOn && m.dl ? 'visible' : 'none');
  }
}
applyOverlayMode();

heatBtn.addEventListener('click', () => {
  const wasOff = overlayModeIdx === 0;
  overlayModeIdx = (overlayModeIdx + 1) % OVERLAY_MODES.length;
  localStorage.setItem('overlay-mode', overlayModeIdx);
  applyOverlayMode();
  if (wasOff) scheduleCommunityFetch(0);   // a no-op until the community block below has run, or when sharing is off
});

// Kept in sync with the Signal Report Stream's date filter (see filter-apply/filter-clear
// below) so the downlink heatmap shows the same time range as everything else on the page,
// instead of always the full unfiltered history.
let dlFilterFromTs = null;
let dlFilterToTs   = null;

function fetchDlOverlay() {
  if (fileMode) return;
  const range = (dlFilterFromTs !== null) ? `&from_ts=${dlFilterFromTs}&to_ts=${dlFilterToTs}` : '';
  fetch(`/api/geojson?direction=down${range}`)
    .then(r => r.json())
    .then(fc => {
      heatDlFeatures = (fc.features || []).map(f => heatFeature(
        f.geometry.coordinates[1],
        f.geometry.coordinates[0],
        rssiToIntensity(f.properties.rssi),
      ));
      syncHeat();
    })
    .catch(() => {});
}
fetchDlOverlay();
setInterval(fetchDlOverlay, 30000);

// RSSI range: -50 dBm (strong) → 1.0, -120 dBm (very weak) → floor.
// RNode sometimes reports the -157 dBm floor when RSSI is missing; treat that
// (and any value below -150) as "unknown" so the point still paints on the map.
function rssiToIntensity(rssi) {
  if (rssi === null || rssi <= -150) return 0.35;
  return Math.min(1, Math.max(0.15, (rssi + 120) / 70));
}

let lastTs       = 0;
let total        = 0;
let fitDone      = {{ fit_done }};
const DEFAULT_COUNTRY = {{ default_country | safe }};
let filterActive = false;
let pollTimer    = null;
const bounds     = [];
const allPoints  = [];

const hoverTip  = document.getElementById('hover-tip');

function pctToColor(pct) {
  const t = Math.min(1, Math.max(0, pct / 100));
  if (t < 0.5) {
    const r = Math.round(204 + (204 - 204) * (t * 2));
    const g = Math.round(51  + (204 - 51)  * (t * 2));
    return `rgb(${r},${g},51)`;
  } else {
    const r = Math.round(204 - 204 * ((t - 0.5) * 2));
    const g = Math.round(204 - 51  * ((t - 0.5) * 2));
    return `rgb(${r},${g},51)`;
  }
}

// Fills one gauge row (idPrefix '' = uplink, 'dl-' = downlink) from rssi/snr, which may be
// null/undefined (no reading for this direction on this point) — rendered as empty/grey.
function setGaugeRow(idPrefix, rssi, snr) {
  const hasRssi = rssi !== null && rssi !== undefined;
  const hasSnr  = snr  !== null && snr  !== undefined;
  const rssiPct = hasRssi ? Math.min(100, Math.max(0, (rssi + 110) / 80 * 100)) : 0;
  const snrPct  = hasSnr  ? Math.min(100, Math.max(0, (snr  +  20) / 30 * 100)) : 0;
  document.getElementById(`hover-${idPrefix}rssi-mask`).style.height = (100 - rssiPct) + '%';
  document.getElementById(`hover-${idPrefix}snr-mask`).style.height  = (100 - snrPct)  + '%';
  const rssiEl  = document.getElementById(`hover-${idPrefix}rssi-val`);
  const snrEl   = document.getElementById(`hover-${idPrefix}snr-val`);
  const rssiLbl = rssiEl.nextElementSibling;
  const snrLbl  = snrEl.nextElementSibling;
  rssiEl.textContent = hasRssi ? rssi : '';
  snrEl.textContent  = hasSnr  ? snr  : '';
  const rc = hasRssi ? pctToColor(rssiPct) : '#555';
  const sc = hasSnr  ? pctToColor(snrPct)  : '#555';
  rssiEl.style.color = rc; rssiLbl.style.color = rc;
  snrEl.style.color  = sc; snrLbl.style.color  = sc;
}

const hoverBarsUl = document.getElementById('hover-bars');
const hoverBarsDl = document.getElementById('hover-bars-dl');

// showUl/showDl reflect the current overlay mode; the downlink row additionally only shows
// when the hovered point actually has a downlink reading (not every point does).
function showHoverTip(p, cx, cy, showUl, showDl) {
  if (showUl) { setGaugeRow('', p.rssi, p.snr); hoverBarsUl.style.display = 'flex'; }
  else hoverBarsUl.style.display = 'none';

  const hasDl = p.dl_rssi !== null && p.dl_rssi !== undefined;
  if (showDl && hasDl) {
    setGaugeRow('dl-', p.dl_rssi, p.dl_snr);
    hoverBarsDl.style.display = 'flex';
    // Only separate it from the uplink row when that row is actually shown above it —
    // in Downlink-only mode there's nothing to divide from.
    hoverBarsDl.classList.toggle('with-divider', showUl);
  } else {
    hoverBarsDl.style.display = 'none';
  }

  hoverTip.style.left   = (cx + 14) + 'px';
  hoverTip.style.top    = (cy - 14) + 'px';
  hoverTip.style.display = 'block';
}

// The closest reading to the cursor, from the live stream, an opened file or the community layer.
// There can be tens of thousands of community points and this runs on every mouse move, so a cheap
// geographic window around the cursor is applied before anything is projected to the screen.
function nearestPoint(cursor, predicate) {
  const R = 30;   // pixels
  const corners = [[-R, -R], [R, -R], [-R, R], [R, R]].map(c => map.unproject([cursor.x + c[0], cursor.y + c[1]]));
  const west  = Math.min(...corners.map(c => c.lng)), east  = Math.max(...corners.map(c => c.lng));
  const south = Math.min(...corners.map(c => c.lat)), north = Math.max(...corners.map(c => c.lat));
  let best = null, minDist = R;
  const scan = list => {
    for (const p of list) {
      if (p.lat < south || p.lat > north || p.lon < west || p.lon > east) continue;
      if (predicate && !predicate(p)) continue;
      const pt = map.project([p.lon, p.lat]);
      const d  = Math.hypot(pt.x - cursor.x, pt.y - cursor.y);
      if (d < minDist) { minDist = d; best = p; }
    }
  };
  scan(allPoints);
  scan(filePoints);
  if (COMMUNITY && commOn) scan(commPoints);
  return best;
}
const hasDlReading = p => p.dl_rssi !== null && p.dl_rssi !== undefined;

map.on('mousemove', e => {
  const cursor = e.point;
  const mode = OVERLAY_MODES[overlayModeIdx];
  if (!mode.ul && !mode.dl) { hoverTip.style.display = 'none'; return; }
  // Whenever downlink is shown at all (Downlink-only or Uplink+Downlink), prefer a point
  // that actually has a downlink reading — only a sparse subset of points do, so searching
  // all points indiscriminately (as Uplink-only does) would almost always land on one
  // without downlink data and never show it. Only fall back to "nearest of anything" (for
  // an uplink-only display) when no downlink-carrying point is nearby.
  let nearest = mode.dl ? nearestPoint(cursor, hasDlReading) : null;
  if (!nearest && mode.ul) nearest = nearestPoint(cursor, null);
  if (nearest) showHoverTip(nearest, e.originalEvent.clientX, e.originalEvent.clientY, mode.ul, mode.dl);
  else hoverTip.style.display = 'none';
});

map.on('mouseout', () => { hoverTip.style.display = 'none'; });

const panelList = document.getElementById('panel-list');

function makeHashSpan(hash) {
  const span = document.createElement('span');
  span.className = 'entry-hash';
  span.textContent = hash;
  span.addEventListener('click', (e) => {
    e.stopPropagation();
    navigator.clipboard.writeText(hash);
    span.textContent = t('misc.copied', 'Copied!');
    setTimeout(() => { span.textContent = hash; }, 1000);
  });
  return span;
}
const MAX_ENTRIES = 50;

function addPanelEntry(p) {
  const d   = new Date(p.ts * 1000);
  const tm  = d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});   // not `t`: that shadowed the translate function below
  const dt  = d.toLocaleString([], {year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  const lat = p.lat.toFixed(5);
  const lon = p.lon.toFixed(5);

  function mkBar(pct, text, isQ = false) {
    const p = Math.min(100, Math.max(0, pct));
    const cls = isQ ? 'rf-bar rf-bar-q' : 'rf-bar';
    return `<span class="${cls}"><span class="rf-mask" style="width:${100-p}%"></span><span class="rf-text">${text}</span></span>`;
  }

  let rf = '';
  if (p.rssi !== null) rf += `<span class="rf-label">RSSI</span>` + mkBar((p.rssi + 110) / 80 * 100, `${p.rssi}dBm`) + ' ';
  if (p.snr  !== null) rf += `<span class="rf-label">SNR</span>`  + mkBar((p.snr  +  20) / 30 * 100, `${p.snr}dB`) + ' ';
  if (p.q    !== null) rf += `<span class="rf-label" style="margin-left:auto">Q</span>` + mkBar(p.q, `${p.q}%`, true);

  const div = document.createElement('div');
  div.className = 'entry';
  div.innerHTML =
    `<div class="entry-time"><span class="ts" data-date="${dt}">${tm}</span> <span class="entry-from">${t('stream.from', 'from')} </span></div>` +
    `<div style="display:flex;align-items:center">${rf}</div>` +
    `<div style="display:flex;align-items:center;justify-content:space-between">` +
      `<div class="entry-coords" data-lat="${p.lat}" data-lon="${p.lon}">${lat}, ${lon}</div>` +
      `<span class="src-pill src-${p.source || 'ann'}">${p.source === 'pkt' ? t('src.pkt', 'Packet') : p.source === 'relay' ? t('src.relay', 'Relay') : p.source === 'file' ? t('src.file', 'File') : t('src.ann', 'Announce')}</span>` +
    `</div>`;
  div.querySelector('.entry-from').appendChild(makeHashSpan(p.sender_hash));
  div.querySelector('.entry-coords').addEventListener('click', () => focusOn(p.lat, p.lon, 15));

  panelList.prepend(div);

  while (panelList.children.length > MAX_ENTRIES)
    panelList.removeChild(panelList.lastChild);
}

// Leaflet's setView() was instant, so this uses jumpTo() rather than an eased flight.
function focusOn(lat, lon, minZoom) {
  map.jumpTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), toMapZoom(minZoom)) });
}

function resetView() {
  heatUlFeatures = [];
  syncHeat();
  panelList.innerHTML = '';
  bounds.length     = 0;
  allPoints.length  = 0;
  filePoints.length = 0;
  total   = 0;
  fitDone = false;
}

const _senderFilter = (() => {
  try {
    return String(new URLSearchParams(location.search).get('sender') || '')
      .replace(/[^0-9a-fA-F]/g, '').toLowerCase();
  } catch (e) {
    return '';
  }
})();

function withSender(url) {
  if (!_senderFilter) return url;
  return url + (url.includes('?') ? '&' : '?') + 'sender=' + encodeURIComponent(_senderFilter);
}

function loadPoints(url) {
  fetch(withSender(url))
    .then(r => r.json())
    .then(pts => {
      if (pts.length === 0) return;

      pts.forEach(p => {
        heatUlFeatures.push(heatFeature(p.lat, p.lon, rssiToIntensity(p.rssi)));
        bounds.push([p.lat, p.lon]);
        allPoints.push(p);
        if (p.ts > lastTs) lastTs = p.ts;
        addPanelEntry(p);
      });

      syncHeat();
      total += pts.length;
      const lastD  = new Date(lastTs * 1000);
      const ts     = lastD.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
      const tsFull = lastD.toLocaleString([], {year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
      const statusTime = document.getElementById('status-time');
      statusTime.textContent  = (filterActive ? t('status.filtered', 'Filtered to ')
                                              : t('status.last', 'Last announce at ')) + ts;
      statusTime.dataset.date = tsFull;
      document.getElementById('status-total').textContent =
        t('status.total', 'Total: ') + total +
        (uiLang === 'ru' ? t('status.points', ' points')
                         : ' point' + (total !== 1 ? 's' : ''));

      if (!fitDone && bounds.length > 0) {
        if (bounds.length === 1) {
          map.jumpTo({ center: [bounds[0][1], bounds[0][0]], zoom: toMapZoom(15) });
        } else {
          const b = new maplibregl.LngLatBounds();
          bounds.forEach(([lat, lon]) => b.extend([lon, lat]));
          map.fitBounds(b, { padding: 40, animate: false });
        }
        fitDone = true;
      }
    })
    .catch(() => {
      document.getElementById('status-time').textContent = t('status.dbdown', 'DB unavailable — retrying…');
    });
}

function toDateStr(d) {
  return d.getFullYear() + '-' +
    String(d.getMonth() + 1).padStart(2, '0') + '-' +
    String(d.getDate()).padStart(2, '0');
}

document.querySelectorAll('[data-preset]').forEach(btn => {
  btn.addEventListener('click', () => {
    const now   = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    let from;
    switch (btn.dataset.preset) {
      case 'day':   from = today; break;
      case 'week':  from = new Date(today); from.setDate(today.getDate() - 6); break;
      case 'month': from = new Date(today.getFullYear(), today.getMonth(), 1); break;
      case 'year':  from = new Date(today.getFullYear(), 0, 1); break;
    }
    document.getElementById('filter-from').value = toDateStr(from);
    document.getElementById('filter-to').value   = toDateStr(today);
    document.getElementById('filter-apply').click();
  });
});

document.getElementById('filter-apply').addEventListener('click', () => {
  const fromVal = document.getElementById('filter-from').value;
  const toVal   = document.getElementById('filter-to').value;
  if (!fromVal && !toVal) return;
  const fromTs = fromVal ? Math.floor(new Date(fromVal + 'T00:00:00').getTime() / 1000) : 0;
  const toTs   = toVal   ? Math.floor(new Date(toVal   + 'T23:59:59').getTime() / 1000) : Math.floor(Date.now() / 1000);
  fileMode = false;
  fileMissFeatures = [];
  document.getElementById('file-close').style.display = 'none';
  filterActive = true;
  clearInterval(pollTimer);
  resetView();
  lastTs = 0;
  setPanelTitle(SIGNAL_ICON, t('stream.filtered', 'Beacons (filtered)'));
  loadPoints(`/api/points?from_ts=${fromTs}&to_ts=${toTs}`);
  dlFilterFromTs = fromTs;
  dlFilterToTs   = toTs;
  fetchDlOverlay();
});

document.getElementById('filter-clear').addEventListener('click', () => {
  filterActive = false;
  document.getElementById('filter-from').value = '';
  document.getElementById('filter-to').value   = '';
  setPanelTitle(SIGNAL_ICON, t('stream.title', 'Beacons'));
  resetView();
  lastTs = 0;
  dlFilterFromTs = null;
  dlFilterToTs   = null;
  fetchDlOverlay();
  loadPoints('/api/points?since=0');
  pollTimer = setInterval(() => loadPoints('/api/points?since=' + lastTs), 5000);
});

function fetchPoints() {
  if (!filterActive && !fileMode) loadPoints('/api/points?since=' + lastTs);
}

document.getElementById('discovery-close').addEventListener('click', () => {
  document.getElementById('discovery-panel').style.display = 'none';
  document.getElementById('discovery-open-wrap').style.display = 'block';
});
document.getElementById('discovery-open').addEventListener('click', () => {
  document.getElementById('discovery-panel').style.display = 'flex';
  document.getElementById('discovery-open-wrap').style.display = 'none';
});

document.getElementById('panel-close').addEventListener('click', () => {
  document.getElementById('panel').style.display = 'none';
  document.getElementById('panel-open').classList.remove('active');
  map.resize();
});
document.getElementById('panel-open').addEventListener('click', () => {
  const panel = document.getElementById('panel');
  const open = panel.style.display !== 'flex';
  panel.style.display = open ? 'flex' : 'none';
  document.getElementById('nodes-panel').style.display = 'none';
  document.getElementById('panel-open').classList.toggle('active', open);
  document.getElementById('nodes-open').classList.remove('active');
  if (_geoTimer) { clearInterval(_geoTimer); _geoTimer = null; }
  map.resize();
});

function toDateInput(ts) {
  const d = new Date(ts * 1000);
  return d.getFullYear() + '-' +
    String(d.getMonth() + 1).padStart(2, '0') + '-' +
    String(d.getDate()).padStart(2, '0');
}

fetch('/api/range').then(r => r.json()).then(range => {
  if (range.min_ts) document.getElementById('filter-from').value = toDateInput(range.min_ts);
  document.getElementById('filter-to').value = toDateInput(Math.floor(Date.now() / 1000));
});

setTimeout(() => { map.resize(); fetchPoints(); }, 100);
pollTimer = setInterval(fetchPoints, 5000);

// ---- Open an exported file ----
// The rns-wardrive app can export its local samples (GeoJSON, CSV, GPX) with no bot or
// server involved. They are read here, in the browser, and shown in place of the live
// data until the file is closed. Nothing is uploaded. Written without backslashes on
// purpose: this whole block is a non-raw Python string (see check_page_js.py).
const NL = String.fromCharCode(10), CR = String.fromCharCode(13), DQ = String.fromCharCode(34);

function numOrNull(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// Every format is reduced to the same row: ts (s), lat, lon, rssi/snr (uplink),
// dl_rssi/dl_snr, replied (bool), who (responder id or '').
function fileRow(f) {
  const lat = numOrNull(f.lat), lon = numOrNull(f.lon);
  let ts = numOrNull(f.time_ms);
  if (ts !== null) ts = Math.floor(ts / 1000);
  else {
    const d = Date.parse(f.time || f.time_utc || '');
    if (!Number.isFinite(d)) return null;
    ts = Math.floor(d / 1000);
  }
  if (lat === null || lon === null || Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
  const who = f.responder_id ? String(f.responder_id).trim() : '';
  const flag = String(f.replied === undefined || f.replied === null ? '' : f.replied).trim().toLowerCase();
  const replied = flag === 'true' ? true : flag === 'false' ? false : who !== '';
  return { ts, lat, lon, replied, who,
           rssi: numOrNull(f.rssi !== undefined ? f.rssi : f.ul_rssi_dbm),
           snr:  numOrNull(f.snr  !== undefined ? f.snr  : f.ul_snr_db),
           dl_rssi: numOrNull(f.dl_rssi !== undefined ? f.dl_rssi : f.dl_rssi_dbm),
           dl_snr:  numOrNull(f.dl_snr  !== undefined ? f.dl_snr  : f.dl_snr_db) };
}

function parseGeoJson(text) {
  const fc = JSON.parse(text);
  const feats = fc.type === 'FeatureCollection' ? fc.features : [fc];
  return (feats || []).map(ft => {
    const c = ft && ft.geometry && ft.geometry.type === 'Point' ? ft.geometry.coordinates : null;
    return c ? { ...(ft.properties || {}), lon: c[0], lat: c[1] } : {};
  });
}

function csvRecords(text) {
  const rows = [];
  let row = [], cell = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === DQ) { if (text[i + 1] === DQ) { cell += DQ; i++; } else quoted = false; }
      else cell += ch;
    } else if (ch === DQ) quoted = true;
    else if (ch === ',') { row.push(cell); cell = ''; }
    else if (ch === CR) { /* CRLF: the LF ends the row */ }
    else if (ch === NL) { row.push(cell); rows.push(row); row = []; cell = ''; }
    else cell += ch;
  }
  if (cell !== '' || row.length) { row.push(cell); rows.push(row); }
  return rows.filter(r => r.length > 1 || r[0] !== '');
}

function parseCsv(text) {
  const recs = csvRecords(text);
  if (!recs.length) return [];
  const head = recs[0].map(h => h.trim());
  return recs.slice(1).map(r => Object.fromEntries(head.map((h, i) => [h, r[i]])));
}

function parseGpx(text) {
  const doc = new DOMParser().parseFromString(text, 'application/xml');
  if (doc.getElementsByTagName('parsererror').length) throw new Error('bad XML');
  const out = [];
  for (const pt of doc.getElementsByTagName('trkpt')) {
    const x = n => { const e = pt.getElementsByTagName(n)[0]; return e ? e.textContent : null; };
    out.push({ lat: pt.getAttribute('lat'), lon: pt.getAttribute('lon'), time: x('time'),
               replied: x('wd:replied'), responder_id: x('wd:responder'),
               ul_rssi_dbm: x('wd:ul_rssi_dbm'), ul_snr_db: x('wd:ul_snr_db'),
               dl_rssi_dbm: x('wd:dl_rssi_dbm'), dl_snr_db: x('wd:dl_snr_db') });
  }
  return out;
}

function parseExport(name, text) {
  const body = text.replace(/^﻿/, '').trimStart();
  const ext = name.toLowerCase().split('.').pop();
  let raw;
  if (ext === 'csv' || body.startsWith('time_') || body.includes(',lat,')) raw = parseCsv(body);
  else if (ext === 'gpx' || body.startsWith('<')) raw = parseGpx(body);
  else raw = parseGeoJson(body);
  const rows = raw.map(fileRow).filter(r => r !== null);
  return { rows, skipped: raw.length - rows.length };
}

async function openFiles(files) {
  let rows = [], skipped = 0;
  const names = [];
  try {
    for (const f of files) {
      const r = parseExport(f.name, await f.text());
      rows = rows.concat(r.rows);
      skipped += r.skipped;
      names.push(f.name);
    }
  } catch (e) {
    document.getElementById('status-time').textContent = t('file.unreadable', 'Could not read the file: ') + e.message;
    return;
  }
  rows.sort((a, b) => a.ts - b.ts);

  fileMode = true;
  openedFiles = files;
  clearInterval(pollTimer);
  filterActive = false;
  resetView();
  lastTs = rows.length ? rows[rows.length - 1].ts : 0;
  heatUlFeatures = []; heatDlFeatures = []; fileMissFeatures = [];
  for (const r of rows) {
    bounds.push([r.lat, r.lon]);
    if (!r.replied) { fileMissFeatures.push(heatFeature(r.lat, r.lon, 0)); continue; }
    filePoints.push({ lat: r.lat, lon: r.lon, rssi: r.rssi, snr: r.snr, dl_rssi: r.dl_rssi, dl_snr: r.dl_snr });
    heatUlFeatures.push(heatFeature(r.lat, r.lon, rssiToIntensity(r.rssi)));
    if (r.dl_rssi !== null) heatDlFeatures.push(heatFeature(r.lat, r.lon, rssiToIntensity(r.dl_rssi)));
  }
  rows.slice(-MAX_ENTRIES).forEach(r => {
    addPanelEntry({ ts: r.ts, lat: r.lat, lon: r.lon, rssi: r.rssi, snr: r.snr, q: null,
                    sender_hash: r.who || '-', source: 'file' });
  });
  syncHeat();
  // Show something: a file's signal is the whole point of opening it.
  if (overlayModeIdx === 0) { overlayModeIdx = heatDlFeatures.length ? 3 : 1; applyOverlayMode(); }

  setPanelTitle(ICON_FILE, names.join(', '));
  document.getElementById('file-close').style.display = '';
  const statusTime = document.getElementById('status-time');
  statusTime.textContent = names.join(', ');
  statusTime.dataset.date = '';
  document.getElementById('status-total').textContent =
    t('status.total', 'Total: ') + rows.length +
    (uiLang === 'ru' ? t('status.points', ' points') : ' point' + (rows.length !== 1 ? 's' : '')) +
    (fileMissFeatures.length ? ' · ' + fileMissFeatures.length + t('file.holes', ' unanswered') : '') +
    (skipped ? ' · ' + skipped + t('file.skipped', ' rows skipped') : '');

  if (bounds.length) {
    const b = new maplibregl.LngLatBounds();
    bounds.forEach(([lat, lon]) => b.extend([lon, lat]));
    map.fitBounds(b, { padding: 40, animate: false, maxZoom: 17 });
  }
  fitDone = true;
}

document.getElementById('file-open').addEventListener('click', () => document.getElementById('file-input').click());
document.getElementById('file-input').addEventListener('change', e => {
  if (e.target.files.length) openFiles(Array.from(e.target.files));
  e.target.value = '';
});
document.getElementById('file-close').addEventListener('click', () => {
  fileMode = false;
  openedFiles = [];
  fileMissFeatures = [];
  document.getElementById('file-close').style.display = 'none';
  document.getElementById('filter-clear').click();   // back to the live data, same as clearing a filter
});
window.addEventListener('dragover', e => { if (e.dataTransfer && e.dataTransfer.types.includes('Files')) e.preventDefault(); });
window.addEventListener('drop', e => {
  if (!e.dataTransfer || !e.dataTransfer.files.length) return;
  e.preventDefault();
  openFiles(Array.from(e.dataTransfer.files));
});

// ---- Community overlay ----
// Signal reports other people have shared, and the dialog for sharing your own. The
// server decides what is published (it re-parses and vets the raw file); nothing here is
// trusted for that, and every piece of server text is shown with textContent.
let scheduleCommunityFetch = () => {};
if (COMMUNITY) {
  const byId = id => document.getElementById(id);
  const TOKEN_KEY = 'community-token';
  const storeGet = k => { try { return localStorage.getItem(k) || ''; } catch (e) { return ''; } };
  const storeSet = (k, v) => { try { if (v) localStorage.setItem(k, v); else localStorage.removeItem(k); } catch (e) {} };

  // -- the layer --
  let fetchTimer = null, fetchCtl = null;
  scheduleCommunityFetch = delay => { clearTimeout(fetchTimer); fetchTimer = setTimeout(fetchCommunity, delay); };

  function wrapLon(x) { return ((x + 180) % 360 + 360) % 360 - 180; }

  async function fetchCommunity() {
    if (!commOn || !styleReady) return;
    const mode = OVERLAY_MODES[overlayModeIdx];
    if (!mode.ul && !mode.dl) return;   // Signal Overlay is off: nothing is drawn, so nothing to fetch
    const b = map.getBounds();
    let w = b.getWest(), e = b.getEast();
    if (e - w >= 360) { w = -180; e = 180; } else { w = wrapLon(w); e = wrapLon(e); }   // the server reads w > e as crossing 180
    const s = Math.max(-90, b.getSouth()), n = Math.min(90, b.getNorth());
    if (fetchCtl) fetchCtl.abort();
    fetchCtl = new AbortController();
    try {
      const r = await fetch('/api/overlay?bbox=' + [w, s, e, n].map(v => v.toFixed(5)).join(','), { signal: fetchCtl.signal });
      if (!r.ok) return;
      const fc = await r.json();
      commUl = []; commDl = []; commPoints = [];
      for (const f of fc.features) {
        const p = f.properties, c = f.geometry.coordinates;
        if (!p.replied) continue;   // unanswered probes are stored but not drawn: they would trace routes
        commPoints.push({ lat: c[1], lon: c[0], rssi: p.rssi, snr: p.snr, dl_rssi: p.dl_rssi, dl_snr: p.dl_snr });
        if (typeof p.rssi === 'number') commUl.push(heatFeature(c[1], c[0], rssiToIntensity(p.rssi)));
        if (typeof p.dl_rssi === 'number') commDl.push(heatFeature(c[1], c[0], rssiToIntensity(p.dl_rssi)));
      }
      syncHeat();
    } catch (e) { /* aborted by a newer request, or offline: keep what is on screen */ }
  }

  map.on('moveend', () => { if (commOn) scheduleCommunityFetch(700); });

  // -- sharing --
  // English texts; Russian ones are in I18N under pub.err.<code>.
  const ERR = {
    unauthorized:   'That token is not valid, or the account has been disabled.',
    forbidden:      'This account is not allowed to do that.',
    duplicate_file: 'You have already published this exact file.',
    all_duplicates: 'Everything in this file was already published.',
    quota_batches:  'Daily upload limit reached. Try again tomorrow.',
    quota_rows:     'Daily limit on shared readings reached. Try again tomorrow.',
    invite_quota:   'You have used all of your invites.',
    overlay_full:   'The community overlay is full right now.',
    too_large:      'The file is too large to share.',
    empty:          'The file is empty.',
    bad_invite:     'That invite code is not valid, has been used, or has expired.',
    bad_name:       'Enter a name or callsign.',
    bad_request:    'Enter the invite code and a name.',
    no_valid_rows:  'The file has no readings that pass the checks.',
    too_many_invalid: 'Too many readings in the file failed the checks.',
    span_too_long:  'The readings span too long a period for one upload.',
    synthetic:      'The readings look machine-generated.',
    too_many_rows:  'The file has too many rows.',
    not_utf8: 'The file is not text.', bad_json: 'The file is not valid JSON.', bad_csv: 'The file is not valid CSV.',
    bad_xml: 'The file is not valid XML.', xml_dtd_forbidden: 'The file contains a DTD, which is not allowed.',
    bad_geojson: 'The file is not a GeoJSON export.',
    rate_limited:   'Too many requests. Wait a minute and try again.',
    network:        'Could not reach the server.',
  };
  const errText = code => t('pub.err.' + code, ERR[code] || code);

  function say(parts, bad) {
    const out = byId('pub-result');
    out.textContent = '';
    for (const p of parts) {
      const line = document.createElement('div');
      line.className = bad ? 'pub-bad' : 'pub-good';
      if (typeof p === 'string') line.textContent = p;
      else { line.appendChild(document.createTextNode(p.before)); const c = document.createElement('code'); c.textContent = p.code; line.appendChild(c); }
      out.appendChild(line);
    }
  }

  async function post(path, opts) {
    try {
      const r = await fetch(path, opts);
      if (r.status === 429 && !(r.headers.get('content-type') || '').includes('json')) return { status: 429, body: { error: 'rate_limited' } };
      let body = {};
      try { body = await r.json(); } catch (e) {}
      return { status: r.status, body };
    } catch (e) {
      return { status: 0, body: { error: 'network' } };
    }
  }

  const bearer = () => ({ 'Authorization': 'Bearer ' + byId('pub-token').value.trim() });
  const rejectedText = rej => Object.entries(rej || {}).map(([k, v]) => k + ' x' + v).join(', ');

  function openDialog() {
    byId('pub-token').value = storeGet(TOKEN_KEY);
    const names = openedFiles.map(f => f.name);
    byId('pub-files').textContent = names.length ? names.join(', ') : t('pub.nofile', 'Open an exported file first, then share it from here.');
    byId('pub-send').disabled = !names.length;
    byId('pub-result').textContent = '';
    setTokenShown(false);
    byId('pub-dialog').style.display = 'flex';
    byId('pub-token').focus();
  }
  // The eye reveals or masks the token. Masked is the rule: the dialog always opens and closes masked, so
  // a token left on screen for one look is not still showing the next time someone opens it.
  if (_EN_DEFAULTS['pub.hide'] === undefined) _EN_DEFAULTS['pub.hide'] = 'Hide token';
  function setTokenShown(on) {
    const eye = byId('pub-eye');
    byId('pub-token').type = on ? 'text' : 'password';
    eye.classList.toggle('on', on);
    eye.setAttribute('aria-pressed', on ? 'true' : 'false');
    // Keyed so a language switch while the token is showing keeps the right wording.
    const key = on ? 'pub.hide' : 'pub.show';
    eye.setAttribute('data-i18n-title', key);
    eye.title = t(key);
    eye.setAttribute('aria-label', eye.title);
  }
  const closeDialog = () => { byId('pub-dialog').style.display = 'none'; setTokenShown(false); };
  byId('pub-eye').addEventListener('click', () => setTokenShown(byId('pub-token').type === 'password'));

  byId('file-publish').addEventListener('click', openDialog);
  byId('pub-close').addEventListener('click', closeDialog);
  byId('pub-dialog').addEventListener('click', e => { if (e.target === byId('pub-dialog')) closeDialog(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDialog(); });

  byId('pub-forget').addEventListener('click', () => {
    storeSet(TOKEN_KEY, '');
    byId('pub-token').value = '';
    say([t('pub.forgotten', 'Token removed from this browser.')], false);
  });

  byId('pub-redeem').addEventListener('click', async () => {
    const code = byId('pub-code').value.trim(), name = byId('pub-name').value.trim();
    if (!code || !name) { say([errText('bad_request')], true); return; }
    const r = await post('/api/overlay/redeem', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code, name }) });
    if (r.status !== 200) { say([errText(r.body.error)], true); return; }
    byId('pub-token').value = r.body.token;
    storeSet(TOKEN_KEY, r.body.token);
    byId('pub-code').value = '';
    say([{ before: t('pub.welcome', 'Invite accepted. Keep a copy of your token, it is shown only once: '), code: r.body.token }], false);
  });

  byId('pub-invite').addEventListener('click', async () => {
    const r = await post('/api/overlay/invite', { method: 'POST', headers: bearer() });
    if (r.status !== 200) { say([errText(r.body.error)], true); return; }
    say([{ before: t('pub.invited', 'One-time invite code, valid for two weeks: '), code: r.body.code }], false);
  });

  byId('pub-send').addEventListener('click', async () => {
    const token = byId('pub-token').value.trim();
    if (!token) { say([t('pub.needToken', 'Enter your access token first.')], true); return; }
    storeSet(TOKEN_KEY, token);
    byId('pub-send').disabled = true;
    const lines = [];
    let anyBad = false;
    for (const f of openedFiles) {
      // The raw file goes up untouched: the server parses it itself.
      const r = await post('/api/overlay/upload?name=' + encodeURIComponent(f.name),
        { method: 'POST', headers: { ...bearer(), 'Content-Type': 'application/octet-stream' }, body: f });
      const b = r.body;
      if (r.status === 200) {
        lines.push(f.name + ': ' + b.accepted + t('pub.published', ' published') +
          (b.duplicates ? ', ' + b.duplicates + t('pub.dupes', ' already there') : '') +
          (rejectedText(b.rejected) ? ' (' + t('pub.skipped', 'skipped: ') + rejectedText(b.rejected) + ')' : ''));
      } else {
        anyBad = true;
        lines.push(f.name + ': ' + errText(b.error) + (rejectedText(b.rejected) ? ' (' + rejectedText(b.rejected) + ')' : ''));
      }
    }
    say(lines, anyBad);
    byId('pub-send').disabled = !openedFiles.length;
    if (commOn) scheduleCommunityFetch(0);
  });
}

// ---- Return to the gateway's home position ----

// Was an L.Control; MapLibre has no equivalent registry for a one-off button, so it
// is plain DOM parented to the map container (which MapLibre positions relative).
const _followWrap = document.createElement('div');
_followWrap.id = 'follow-control';
const _followBtn = document.createElement('button');
_followBtn.className = 'follow-btn';
_followBtn.id = 'home-btn';
_followBtn.title = t('title.home', 'Return to the gateway location');
_followBtn.setAttribute('aria-label', _followBtn.title);
_followBtn.innerHTML = '<svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 12a3 3 0 1 0 6 0a3 3 0 1 0 -6 0"/><path d="M4 12a8 8 0 1 0 16 0a8 8 0 1 0 -16 0"/><path d="M12 2l0 2"/><path d="M12 20l0 2"/><path d="M20 12l2 0"/><path d="M2 12l2 0"/></svg>';
_followWrap.appendChild(_followBtn);
map.getContainer().appendChild(_followWrap);
_followBtn.addEventListener('click', e => {
  e.preventDefault();
  e.stopPropagation();
  map.flyTo({ center: [HOME_POSITION.lon, HOME_POSITION.lat], zoom: homeZoom(), duration: 500 });
});

// ---- Basemap label suppression ----
// Hides the basemap's own place names, road shields and POI text. This is the runtime
// equivalent of what make_nolabels.py bakes into voyager-nolabels: every symbol layer,
// identified by type rather than by id, so it works across all six basemaps without
// knowing any of their layer names.
//
// The node markers are unaffected -- those are DOM markers rather than GL layers, and
// their captions have their own toggle.
// Labels start on for every page load. Deliberately not persisted: a remembered "off"
// meant the map opened with labels hidden, which is not the state anyone expects to
// arrive in, and turning them back off is one click.
let _geoLabelsOn = true;



const _labelBtn = document.createElement('button');
_labelBtn.className = 'follow-btn';
_labelBtn.innerHTML = '<svg class="ico" aria-hidden="true" fill="none" stroke-width="1.5" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M9.568 3H5.25A2.25 2.25 0 0 0 3 5.25v4.318c0 .597.237 1.17.659 1.591l9.581 9.581c.699.699 1.78.872 2.607.33a18.095 18.095 0 0 0 5.223-5.223c.542-.827.369-1.908-.33-2.607L11.16 3.66A2.25 2.25 0 0 0 9.568 3Z" stroke-linecap="round" stroke-linejoin="round"/><path d="M6 6h.008v.008H6V6Z" stroke-linecap="round" stroke-linejoin="round"/></svg>';
_followWrap.appendChild(_labelBtn);
// Raster tiles have their labels painted into the image; there is nothing to hide.
if (RASTER) _labelBtn.style.display = 'none';

// ---- Ruler ----
// Same tool as in the wardrive app: switch it on, click two points, read the distance
// (great-circle, in metres or kilometres). A third click starts a new measurement. Turning
// it off removes the line. The colour follows the page theme: amber on dark maps, a deep
// orange on light ones where amber washes out.
const RULER_ICON_ON = '<svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3l4 4l-14 14l-4 -4l14 -14"/><path d="M16 7l-1.5 -1.5"/><path d="M13 10l-1.5 -1.5"/><path d="M10 13l-1.5 -1.5"/><path d="M7 16l-1.5 -1.5"/></svg>';
const RULER_ICON_OFF = '<svg class="ico" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.03 7.97l4.97 -4.97l4 4l-5 5m-2 2l-7 7l-4 -4l7 -7"/><path d="M16 7l-1.5 -1.5"/><path d="M10 13l-1.5 -1.5"/><path d="M7 16l-1.5 -1.5"/><path d="M3 3l18 18"/></svg>';
let _rulerOn = false;
let _rulerPts = [];

const _rulerBtn = document.createElement('button');
_rulerBtn.className = 'follow-btn';
_followWrap.appendChild(_rulerBtn);
const _rulerReadout = document.createElement('div');
_rulerReadout.id = 'ruler-readout';
// Two lines: the distance, and under it the bearings (a separate line, labelled).
const _rulerText = document.createElement('div');
const _rulerLine1 = document.createElement('div');
const _rulerLine2 = document.createElement('div');
_rulerText.appendChild(_rulerLine1);
_rulerText.appendChild(_rulerLine2);
const _rulerReset = document.createElement('button');
_rulerReadout.appendChild(_rulerText);
_rulerReadout.appendChild(_rulerReset);
map.getContainer().appendChild(_rulerReadout);

function rulerColor() {
  return document.body.classList.contains('light-theme') ? '#D84315' : '#FFC107';
}

function rulerMeters(a, b) {
  const R = 6371008.8, rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad, dLon = (b.lng - a.lng) * rad;
  const h = Math.sin(dLat / 2) * Math.sin(dLat / 2)
          + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
}

function rulerFormat(m) {
  if (m < 1000) return Math.round(m) + ' ' + t('ruler.m', 'm');
  return (m < 10000 ? (m / 1000).toFixed(2) : (m / 1000).toFixed(1)) + ' ' + t('ruler.km', 'km');
}

// The distance, written along the middle of the dashed line and a little above it. A DOM marker rather
// than a symbol layer: a symbol layer needs the basemap's glyphs, which a raster basemap does not have.
// The outer element is the marker (MapLibre positions it); the inner span is turned to run along the line.
// Fills [el] with [text]: a string, or a list of [string, css class or ''] pieces with their own colours.
function setLabelParts(el, text) {
  if (typeof text === 'string') {
    el.textContent = text;
    return;
  }
  el.textContent = '';
  text.forEach(piece => {
    const part = document.createElement('span');
    part.style.display = 'inline';
    part.style.padding = '0';
    part.style.border = '0';
    part.style.boxShadow = 'none';
    part.style.background = 'none';
    if (piece[1]) part.className = piece[1];
    part.textContent = piece[0];
    el.appendChild(part);
  });
}

function makeLineLabel() {
  const el = document.createElement('div');
  el.className = 'ruler-label';
  const span = document.createElement('span');
  el.appendChild(span);
  return { el: el, span: span, marker: null };
}

function removeLineLabel(L) {
  if (L.marker) { L.marker.remove(); L.marker = null; }
}

// A small arrowhead on the destination end of a ruler, so the two ends can be told apart. A DOM marker like the
// labels, turned to the line's direction on screen (worked out again on every map move, so it follows rotation).
function makeArrow() {
  const el = document.createElement('div');
  el.className = 'ruler-arrow';
  el.innerHTML = '<svg width="14" height="10" viewBox="0 0 14 10" aria-hidden="true" style="display:block;overflow:visible"><path d="M0 0L14 5L0 10Z" stroke="#fff" stroke-width="1" stroke-linejoin="round"/></svg>';
  return { el: el, svg: el.firstChild, path: el.querySelector('path'), marker: null };
}

function removeArrow(A) {
  if (A.marker) { A.marker.remove(); A.marker = null; }
}

// The arrow points from a to b with its tip [gap] pixels short of b, which leaves room for b's dot.
function placeArrow(A, a, b, gap) {
  const pa = map.project(a), pb = map.project(b);
  const angle = Math.atan2(pb.y - pa.y, pb.x - pa.x) * 180 / Math.PI;
  A.path.setAttribute('fill', rulerColor());
  A.svg.style.transform = 'rotate(' + angle + 'deg) translateX(' + (-(gap + 7)) + 'px)';
  if (!A.marker) {
    A.marker = new maplibregl.Marker({ element: A.el, anchor: 'center' }).setLngLat(b).addTo(map);
  } else {
    A.marker.setLngLat(b);
  }
}

// Writes [text] along the line a-b: at its middle, turned to run along it and 14px above it.
// [text] is a string, or a list of [string, css class or ''] pieces so parts can have their own colour.
// [side] is -1 for above the line (the default) and 1 for below it.
function placeLineLabel(L, a, b, text, side) {
  const pa = map.project(a), pb = map.project(b);
  let angle = Math.atan2(pb.y - pa.y, pb.x - pa.x) * 180 / Math.PI;
  if (angle > 90) angle -= 180; else if (angle < -90) angle += 180;   // never upside down
  setLabelParts(L.span, text);
  // Turn first, then move along the turned "up" axis: that is the side above the line.
  L.span.style.transform = 'rotate(' + angle + 'deg) translateY(' + (side === 1 ? 14 : -14) + 'px)';
  const mid = { lng: (a.lng + b.lng) / 2, lat: (a.lat + b.lat) / 2 };
  if (!L.marker) {
    L.marker = new maplibregl.Marker({ element: L.el, anchor: 'center' }).setLngLat(mid).addTo(map);
  } else {
    L.marker.setLngLat(mid);
  }
}

const _rulerLabel = makeLineLabel();       // the distance, above the line
const _rulerLabelBearing = makeLineLabel();    // the bearings and compass point, below it
const _rulerArrow = makeArrow();

function placeRulerLabel() {
  if (_rulerPts.length < 2 || rulerMeters(_rulerPts[0], _rulerPts[1]) < 1) {
    removeLineLabel(_rulerLabel);
    removeLineLabel(_rulerLabelBearing);
    removeArrow(_rulerArrow);
    return;
  }
  placeArrow(_rulerArrow, _rulerPts[0], _rulerPts[1], 6);   // 6 = the second dot's radius plus a pixel
  placeLineLabel(_rulerLabel, _rulerPts[0], _rulerPts[1], rulerFormat(rulerMeters(_rulerPts[0], _rulerPts[1])), -1);
  placeLineLabel(_rulerLabelBearing, _rulerPts[0], _rulerPts[1], bearingParts(_rulerPts[0], _rulerPts[1]), 1);
}
// The line is straight on screen only for the current view, so the angle is worked out again on every move.
map.on('move', () => { if (_rulerPts.length === 2) placeRulerLabel(); if (_qrAnchor) placeQuickLabel(); });

function setupRulerLayers() {
  if (!map.getSource('ruler-line')) {
    map.addSource('ruler-line', { type: 'geojson', data: emptyFC() });
    map.addSource('ruler-points', { type: 'geojson', data: emptyFC() });
  }
  if (!map.getSource('qruler-line')) {
    map.addSource('qruler-line', { type: 'geojson', data: emptyFC() });
    map.addSource('qruler-point', { type: 'geojson', data: emptyFC() });
    map.addSource('qruler-dest', { type: 'geojson', data: emptyFC() });
  }
  if (!map.getLayer('qruler-line')) {
    map.addLayer({ id: 'qruler-line', type: 'line', source: 'qruler-line',
      paint: { 'line-color': rulerColor(), 'line-width': 2, 'line-dasharray': [1, 2] } });
  }
  if (!map.getLayer('qruler-point')) {
    map.addLayer({ id: 'qruler-point', type: 'circle', source: 'qruler-point',
      paint: { 'circle-radius': 5, 'circle-color': rulerColor(),
               'circle-stroke-width': 2, 'circle-stroke-color': '#fff' } });
  }
  if (!map.getLayer('qruler-dest')) {
    map.addLayer({ id: 'qruler-dest', type: 'circle', source: 'qruler-dest',
      paint: { 'circle-radius': 3.5, 'circle-color': rulerColor(),
               'circle-stroke-width': 1.5, 'circle-stroke-color': '#fff' } });
  }
  if (!map.getLayer('ruler-line')) {
    map.addLayer({ id: 'ruler-line', type: 'line', source: 'ruler-line',
      paint: { 'line-color': rulerColor(), 'line-width': 3, 'line-dasharray': [2, 1.5] } });
  }
  if (!map.getLayer('ruler-points')) {
    map.addLayer({ id: 'ruler-points', type: 'circle', source: 'ruler-points',
      // The second dot is smaller, so the start and the destination can be told apart.
      paint: { 'circle-radius': ['case', ['==', ['get', 'dest'], 1], 5, 7], 'circle-color': rulerColor(),
               'circle-stroke-width': 2, 'circle-stroke-color': '#fff' } });
  }
}

// Idempotent, like applyGeoLabels: refreshes the button, readout and map layers from the
// current state, and is safe to call before the style has loaded.
function applyRuler() {
  _rulerBtn.innerHTML = _rulerOn ? RULER_ICON_ON : RULER_ICON_OFF;
  _rulerBtn.classList.toggle('active', _rulerOn);
  _rulerBtn.title = _rulerOn ? t('title.rulerOn', 'Ruler on (click to turn off)')
                             : t('title.rulerOff', 'Measure the distance between two points (Ctrl+click the map for a quick ruler)');
  _rulerReset.textContent = t('ruler.reset', 'Reset');
  // Hidden, not removed, so the popup does not change width when the first point is set.
  _rulerReset.style.visibility = _rulerPts.length ? 'visible' : 'hidden';
  _rulerReadout.style.display = _rulerOn ? 'flex' : 'none';
  _rulerLine2.style.display = 'none';
  if (_rulerPts.length === 0) setLabelParts(_rulerLine1, t('ruler.first', 'Ruler: click the first point'));
  else if (_rulerPts.length === 1) setLabelParts(_rulerLine1, t('ruler.second', 'Ruler: click the second point'));
  else {
    const m = rulerMeters(_rulerPts[0], _rulerPts[1]);
    setLabelParts(_rulerLine1, t('ruler.distance', 'Distance:') + ' ' + rulerFormat(m));
    if (m >= 1) {
      setLabelParts(_rulerLine2, [[t('ruler.bearing', 'Bearing:') + ' ', '']].concat(bearingParts(_rulerPts[0], _rulerPts[1])));
      _rulerLine2.style.display = '';
    }
  }
  map.getCanvas().style.cursor = _rulerOn ? 'crosshair' : '';
  try { placeRulerLabel(); } catch (e) { console.warn('ruler label failed:', e); }
  try {
    if (!map.getSource('ruler-line')) return;
    const color = rulerColor();
    map.setPaintProperty('ruler-line', 'line-color', color);
    map.setPaintProperty('ruler-points', 'circle-color', color);
    map.getSource('ruler-points').setData({ type: 'FeatureCollection', features: _rulerPts.map((p, i) => (
      { type: 'Feature', properties: { dest: i === 1 ? 1 : 0 }, geometry: { type: 'Point', coordinates: [p.lng, p.lat] } })) });
    map.getSource('ruler-line').setData(_rulerPts.length === 2
      ? { type: 'Feature', properties: {}, geometry: { type: 'LineString',
          coordinates: _rulerPts.map(p => [p.lng, p.lat]) } }
      : emptyFC());
  } catch (e) {
    console.warn('ruler update failed:', e);
  }
}

_rulerBtn.addEventListener('click', e => {
  e.preventDefault();
  e.stopPropagation();
  _rulerOn = !_rulerOn;
  _rulerPts = [];
  endQuickRuler();
  applyRuler();
});
_rulerReset.addEventListener('click', e => {
  e.stopPropagation();
  _rulerPts = [];
  applyRuler();
});
map.on('click', e => {
  if (!_rulerOn) return;
  _rulerPts = _rulerPts.length >= 2 ? [e.lngLat] : _rulerPts.concat([e.lngLat]);
  applyRuler();
});

// ---- Magnetic declination ----
// World Magnetic Model 2025 (NOAA/NCEI and the British Geological Survey): epoch 2025.0, valid to 2030.0,
// degree and order 12. The coefficients are the table from NOAA's WMM.COF (n,m,g,h,gdot,hdot); the code
// reproduces NOAA's published WMM2025 test values to within 0.005 degrees. After 2030 it keeps
// extrapolating the secular variation, so it slowly drifts until the model is replaced.
const WMM_EPOCH = 2025.0;
const WMM_COEF = (function () {
  const out = [];
  '1,0,-29351.8,0.0,12.0,0.0;1,1,-1410.8,4545.4,9.7,-21.5;2,0,-2556.6,0.0,-11.6,0.0;2,1,2951.1,-3133.6,-5.2,-27.7;2,2,1649.3,-815.1,-8.0,-12.1;3,0,1361.0,0.0,-1.3,0.0;3,1,-2404.1,-56.6,-4.2,4.0;3,2,1243.8,237.5,0.4,-0.3;3,3,453.6,-549.5,-15.6,-4.1;4,0,895.0,0.0,-1.6,0.0;4,1,799.5,278.6,-2.4,-1.1;4,2,55.7,-133.9,-6.0,4.1;4,3,-281.1,212.0,5.6,1.6;4,4,12.1,-375.6,-7.0,-4.4;5,0,-233.2,0.0,0.6,0.0;5,1,368.9,45.4,1.4,-0.5;5,2,187.2,220.2,0.0,2.2;5,3,-138.7,-122.9,0.6,0.4;5,4,-142.0,43.0,2.2,1.7;5,5,20.9,106.1,0.9,1.9;6,0,64.4,0.0,-0.2,0.0;6,1,63.8,-18.4,-0.4,0.3;6,2,76.9,16.8,0.9,-1.6;6,3,-115.7,48.8,1.2,-0.4;6,4,-40.9,-59.8,-0.9,0.9;6,5,14.9,10.9,0.3,0.7;6,6,-60.7,72.7,0.9,0.9;7,0,79.5,0.0,-0.0,0.0;7,1,-77.0,-48.9,-0.1,0.6;7,2,-8.8,-14.4,-0.1,0.5;7,3,59.3,-1.0,0.5,-0.8;7,4,15.8,23.4,-0.1,0.0;7,5,2.5,-7.4,-0.8,-1.0;7,6,-11.1,-25.1,-0.8,0.6;7,7,14.2,-2.3,0.8,-0.2;8,0,23.2,0.0,-0.1,0.0;8,1,10.8,7.1,0.2,-0.2;8,2,-17.5,-12.6,0.0,0.5;8,3,2.0,11.4,0.5,-0.4;8,4,-21.7,-9.7,-0.1,0.4;8,5,16.9,12.7,0.3,-0.5;8,6,15.0,0.7,0.2,-0.6;8,7,-16.8,-5.2,-0.0,0.3;8,8,0.9,3.9,0.2,0.2;9,0,4.6,0.0,-0.0,0.0;9,1,7.8,-24.8,-0.1,-0.3;9,2,3.0,12.2,0.1,0.3;9,3,-0.2,8.3,0.3,-0.3;9,4,-2.5,-3.3,-0.3,0.3;9,5,-13.1,-5.2,0.0,0.2;9,6,2.4,7.2,0.3,-0.1;9,7,8.6,-0.6,-0.1,-0.2;9,8,-8.7,0.8,0.1,0.4;9,9,-12.9,10.0,-0.1,0.1;10,0,-1.3,0.0,0.1,0.0;10,1,-6.4,3.3,0.0,0.0;10,2,0.2,0.0,0.1,-0.0;10,3,2.0,2.4,0.1,-0.2;10,4,-1.0,5.3,-0.0,0.1;10,5,-0.6,-9.1,-0.3,-0.1;10,6,-0.9,0.4,0.0,0.1;10,7,1.5,-4.2,-0.1,0.0;10,8,0.9,-3.8,-0.1,-0.1;10,9,-2.7,0.9,-0.0,0.2;10,10,-3.9,-9.1,-0.0,-0.0;11,0,2.9,0.0,0.0,0.0;11,1,-1.5,0.0,-0.0,-0.0;11,2,-2.5,2.9,0.0,0.1;11,3,2.4,-0.6,0.0,-0.0;11,4,-0.6,0.2,0.0,0.1;11,5,-0.1,0.5,-0.1,-0.0;11,6,-0.6,-0.3,0.0,-0.0;11,7,-0.1,-1.2,-0.0,0.1;11,8,1.1,-1.7,-0.1,-0.0;11,9,-1.0,-2.9,-0.1,0.0;11,10,-0.2,-1.8,-0.1,0.0;11,11,2.6,-2.3,-0.1,0.0;12,0,-2.0,0.0,0.0,0.0;12,1,-0.2,-1.3,0.0,-0.0;12,2,0.3,0.7,-0.0,0.0;12,3,1.2,1.0,-0.0,-0.1;12,4,-1.3,-1.4,-0.0,0.1;12,5,0.6,-0.0,-0.0,-0.0;12,6,0.6,0.6,0.1,-0.0;12,7,0.5,-0.1,-0.0,-0.0;12,8,-0.1,0.8,0.0,0.0;12,9,-0.4,0.1,0.0,-0.0;12,10,-0.2,-1.0,-0.1,-0.0;12,11,-1.3,0.1,-0.0,0.0;12,12,-0.7,0.2,-0.1,-0.1'.split(';').forEach(r => { const p = r.split(',').map(Number); out.push(p); });
  return out;
})();

// Magnetic declination in degrees (east positive) at a geodetic position, for a date.
function wmmDeclination(latDeg, lonDeg, altKm, date) {
  const rad = Math.PI / 180;
  const a = 6378.137, f = 1 / 298.257223563, e2 = f * (2 - f), re = 6371.2;
  const year = date.getUTCFullYear() + (date - Date.UTC(date.getUTCFullYear(), 0, 1)) /
    (Date.UTC(date.getUTCFullYear() + 1, 0, 1) - Date.UTC(date.getUTCFullYear(), 0, 1));
  const dt = year - WMM_EPOCH;
  const lat = latDeg * rad, lon = lonDeg * rad;
  // geodetic -> geocentric spherical
  const rc = a / Math.sqrt(1 - e2 * Math.sin(lat) * Math.sin(lat));
  const p = (rc + altKm) * Math.cos(lat), z = (rc * (1 - e2) + altKm) * Math.sin(lat);
  const r = Math.sqrt(p * p + z * z), phi = Math.asin(z / r);
  const sp = Math.sin(phi), cp = Math.cos(phi);
  const N = 12;
  const P = [], dP = [];
  for (let n = 0; n <= N; n++) { P.push(new Array(N + 1).fill(0)); dP.push(new Array(N + 1).fill(0)); }
  P[0][0] = 1;
  for (let n = 1; n <= N; n++) {
    for (let m = 0; m <= n; m++) {
      if (n === m) {
        const k = n === 1 ? 1 : Math.sqrt((2 * n - 1) / (2 * n));
        P[n][n] = k * cp * P[n - 1][n - 1];
        dP[n][n] = k * (cp * dP[n - 1][n - 1] - sp * P[n - 1][n - 1]);
      } else {
        const k2 = n - 2 >= m ? Math.sqrt((n - 1) * (n - 1) - m * m) : 0;
        const d = Math.sqrt(n * n - m * m);
        const p2 = n - 2 >= m ? P[n - 2][m] : 0, dp2 = n - 2 >= m ? dP[n - 2][m] : 0;
        P[n][m] = ((2 * n - 1) * sp * P[n - 1][m] - k2 * p2) / d;
        dP[n][m] = ((2 * n - 1) * (cp * P[n - 1][m] + sp * dP[n - 1][m]) - k2 * dp2) / d;
      }
    }
  }
  let X = 0, Y = 0, Z = 0;
  const cosm = [], sinm = [];
  for (let m = 0; m <= N; m++) { cosm.push(Math.cos(m * lon)); sinm.push(Math.sin(m * lon)); }
  WMM_COEF.forEach(c => {
    const n = c[0], m = c[1];
    const g = c[2] + dt * c[4], h = c[3] + dt * c[5];
    const ar = Math.pow(re / r, n + 2);
    const t = g * cosm[m] + h * sinm[m];
    X -= ar * t * dP[n][m];
    Y += ar * m * (g * sinm[m] - h * cosm[m]) * P[n][m];
    Z -= ar * (n + 1) * t * P[n][m];
  });
  // Y's 1/cos(lat') is undefined at the poles; the declination is meaningless there anyway.
  if (Math.abs(cp) < 1e-9) return NaN;
  Y /= cp;
  const psi = phi - lat;
  const Xg = X * Math.cos(psi) - Z * Math.sin(psi);
  return Math.atan2(Y, Xg) / rad;
}

// ---- Quick ruler ----
// Ctrl+click anywhere puts down an anchor; a dotted line then follows the cursor, with the distance and
// the compass bearing (degrees clockwise from north) from the anchor to the cursor written along it.
// A left click while it follows the cursor sets a smaller destination dot and leaves the ruler on the
// map; Ctrl+click starts a new one from another anchor. A right click or Esc removes it, whether it is
// still stretching or already set. It stays out of the way of the two-point ruler: that one is switched
// on from its button, and turns this one off.
let _qrAnchor = null;
let _qrCursor = null;
let _qrFixed = false;     // the destination is set: the line no longer follows the cursor
const _qrLabel = makeLineLabel();       // the distance, above the line
const _qrArrow = makeArrow();
const _qrLabelBearing = makeLineLabel();    // the bearings and compass point, below it

function rulerBearing(a, b) {
  const rad = Math.PI / 180;
  const y = Math.sin((b.lng - a.lng) * rad) * Math.cos(b.lat * rad);
  const x = Math.cos(a.lat * rad) * Math.sin(b.lat * rad)
          - Math.sin(a.lat * rad) * Math.cos(b.lat * rad) * Math.cos((b.lng - a.lng) * rad);
  return (Math.atan2(y, x) / rad + 360) % 360;
}

function rulerCompassPoint(deg) {
  const names = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
  const n = names[Math.round(deg / 45) % 8];
  return t('dir.' + n, n);
}

// "82\u00b0T/70\u00b0M (E)": the true bearing from a to b (blue), the magnetic one (green) and the compass
// point of the true one. Magnetic is true minus the declination (east positive) taken at a.
function bearingParts(a, b) {
  const trueBearing = rulerBearing(a, b);
  const decl = wmmDeclination(a.lat, a.lng, 0, new Date());
  const parts = [[Math.round(trueBearing) % 360 + '\u00b0' + t('ruler.true', 'T'), 'rl-true']];
  if (!isNaN(decl)) {
    parts.push(['/', ''],
               [Math.round((trueBearing - decl + 360) % 360) % 360 + '\u00b0' + t('ruler.mag', 'M'), 'rl-mag']);
  }
  parts.push([' (' + rulerCompassPoint(trueBearing) + ')', '']);
  return parts;
}

function placeQuickLabel() {
  const none = () => { removeLineLabel(_qrLabel); removeLineLabel(_qrLabelBearing); removeArrow(_qrArrow); };
  if (!_qrAnchor || !_qrCursor) { none(); return; }
  const m = rulerMeters(_qrAnchor, _qrCursor);
  if (m < 1) { none(); return; }   // no direction yet while the cursor is on the anchor
  // While it stretches the tip is at the cursor; once the destination dot is down the tip stops short of it.
  placeArrow(_qrArrow, _qrAnchor, _qrCursor, _qrFixed ? 5 : 0);
  placeLineLabel(_qrLabel, _qrAnchor, _qrCursor, rulerFormat(m), -1);
  placeLineLabel(_qrLabelBearing, _qrAnchor, _qrCursor, bearingParts(_qrAnchor, _qrCursor), 1);
}

function applyQuickRuler() {
  try { placeQuickLabel(); } catch (e) { console.warn('quick ruler label failed:', e); }
  try {
    if (!map.getSource('qruler-line')) return;
    const color = rulerColor();
    map.setPaintProperty('qruler-line', 'line-color', color);
    map.setPaintProperty('qruler-point', 'circle-color', color);
    map.setPaintProperty('qruler-dest', 'circle-color', color);
    map.getSource('qruler-dest').setData(_qrAnchor && _qrFixed
      ? { type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [_qrCursor.lng, _qrCursor.lat] } }
      : emptyFC());
    map.getSource('qruler-point').setData(_qrAnchor
      ? { type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [_qrAnchor.lng, _qrAnchor.lat] } }
      : emptyFC());
    map.getSource('qruler-line').setData(_qrAnchor && _qrCursor
      ? { type: 'Feature', properties: {}, geometry: { type: 'LineString',
          coordinates: [[_qrAnchor.lng, _qrAnchor.lat], [_qrCursor.lng, _qrCursor.lat]] } }
      : emptyFC());
  } catch (e) {
    console.warn('quick ruler update failed:', e);
  }
}

function endQuickRuler() {
  _qrAnchor = null;
  _qrCursor = null;
  _qrFixed = false;
  applyQuickRuler();
}

map.on('click', e => {
  if (_rulerOn) return;
  if (e.originalEvent.ctrlKey) {
    _qrAnchor = e.lngLat;
    _qrCursor = e.lngLat;
    _qrFixed = false;
    applyQuickRuler();
  } else if (_qrAnchor && !_qrFixed) {
    // Set the destination here and leave the ruler on the map.
    _qrCursor = e.lngLat;
    _qrFixed = true;
    applyQuickRuler();
  }
});
// Right click removes the quick ruler, stretching or set. Only when there is one, so the browser's own
// context menu is otherwise left alone.
map.on('contextmenu', e => {
  if (!_qrAnchor) return;
  e.originalEvent.preventDefault();
  endQuickRuler();
});
map.on('mousemove', e => {
  if (!_qrAnchor || _qrFixed) return;
  _qrCursor = e.lngLat;
  applyQuickRuler();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape' && _qrAnchor) endQuickRuler(); });

applyRuler();

function applyGeoLabels() {
  // The button state is updated first and unconditionally. It used to sit behind an
  // `if (!map.isStyleLoaded()) return`, which is false during the style.load event
  // itself, so the very call meant to initialise the control bailed out before it.
  _labelBtn.classList.toggle('active', _geoLabelsOn);
  _labelBtn.title = _geoLabelsOn ? t('title.hideLabels', 'Hide map labels')
                                 : t('title.showLabels', 'Show map labels');

  // Written to be idempotent and safe to call at any time, rather than tracked with a
  // "have I applied this yet" flag. The flag version marked itself done even when every
  // setLayoutProperty had just thrown -- which is exactly what happens if getStyle()
  // still reports the outgoing style during a swap -- so the retry never fired and a
  // basemap switch silently restored the labels.
  //
  // Comparing against each layer's current visibility means no write happens when
  // nothing needs to change, so this can run on every idle without churn.
  const want = _geoLabelsOn ? 'visible' : 'none';
  let layers;
  try { layers = map.getStyle().layers || []; } catch (e) { return; }
  for (const l of layers) {
    if (l.type !== 'symbol') continue;
    try {
      if ((map.getLayoutProperty(l.id, 'visibility') || 'visible') !== want) {
        map.setLayoutProperty(l.id, 'visibility', want);
      }
    } catch (e) { /* layer went away mid-swap; the next idle re-checks it */ }
  }
}

// ---- Label language ----
// The styles stack name:latin over name:nonlatin, so every place renders in both
// scripts at once -- Moscow above the Cyrillic. This picks one from the browser's
// language preference instead.
//
// The suffixes below are the name:<code> fields the tileset actually carries (from its
// vector_layers metadata); asking for one it does not have yields an empty label rather
// than a fallback, so the list is matched against rather than trusted from the browser.
const NAME_LANGS = ['af','am','ar','az','be','bg','bn','br','bs','ca','co','cs','cy','da',
  'de','el','en','eo','es','et','eu','fa','fi','fr','fy','ga','gd','he','hi','hr','hu',
  'hy','id','is','it','ja','ka','kk','kn','ko','ko-Latn','ku','la','lb','lt','lv','mk',
  'ml','mt','nl','no','oc','pa','pl','pnb','pt','rm','ro','ru','sk','sl','sq','sr',
  'sr-Latn','sv','ta','te','th','tr','uk','ur','vi','zh','zh-Hans','zh-Hant'];
const _byLower = new Map(NAME_LANGS.map(c => [c.toLowerCase(), c]));

function pickNameLang() {
  const tags = navigator.languages && navigator.languages.length
    ? navigator.languages : [navigator.language || 'en'];
  for (const tag of tags) {
    const low = String(tag).toLowerCase();
    // Try the full tag before the base, so zh-Hant beats zh for a zh-Hant browser.
    for (const cand of [low, low.split('-')[0]]) {
      if (_byLower.has(cand)) return _byLower.get(cand);
    }
  }
  return null;
}

// An explicit choice from the dashboard outranks the browser header: it is a stronger
// signal than what the browser advertises. The choice is
// browser advertises. Persisted so an embedded reload does not flash the other script
// before the parent's message arrives.
function resolveNameLang() {
  const saved = localStorage.getItem('label-lang');
  if (saved && _byLower.has(saved.toLowerCase())) return _byLower.get(saved.toLowerCase());
  return pickNameLang();
}

let _nameLang = resolveNameLang();
// name:latin is the fallback rather than name, because name is whatever the local
// mapper wrote and can be in any script -- which is the thing being avoided here.
// Languages written in a non-Latin script. The fallback order has to differ for these:
// OSM tags name:ru on most Moscow streets but hardly anywhere else in Russia, so a chain
// ending in name:latin quietly turned Lipetsk into "ulica Germana Titova" while Moscow
// stayed Cyrillic. name:nonlatin carries the local name's non-Latin form and is present
// on effectively every Russian street, so it belongs ahead of the transliteration.
const NONLATIN_LANGS = new Set([
  'am','ar','be','bg','bn','el','fa','he','hi','hy','ja','ka','kk','kn','ko','ml','mk',
  'pa','pnb','ru','sr','ta','te','th','uk','ur','zh','zh-Hans','zh-Hant',
]);

function nameExprFor(lang) {
  if (!lang) return ['coalesce', ['get', 'name:latin'], ['get', 'name']];
  if (NONLATIN_LANGS.has(lang)) {
    // name before name:latin as well: a place with no non-Latin form at all is better
    // shown under its own name than transliterated from it.
    return ['coalesce', ['get', 'name:' + lang], ['get', 'name:nonlatin'],
                        ['get', 'name'], ['get', 'name:latin']];
  }
  return ['coalesce', ['get', 'name:' + lang], ['get', 'name:latin'], ['get', 'name']];
}
let NAME_EXPR = nameExprFor(_nameLang);
let _NAME_EXPR_JSON = JSON.stringify(NAME_EXPR);

// Called when the parent page reports its language. Unknown codes are ignored rather
// than applied, because asking the tiles for a name:<code> they do not carry renders
// an empty label instead of falling back.
// True once the initial pass has run, so the first setLabelLang call cannot trigger a
// style reload on top of the load that is already happening.
let _labelLangReady = false;

// Rebuild the style from scratch to flush stale labels.
//
// setLayoutProperty on text-field re-lays out the symbol layers, but a tile's labels are
// built when the tile is parsed, and MapLibre keeps already-parsed tiles. Switching
// language therefore repainted whatever it happened to re-parse and left the rest alone,
// which is the mixed Latin/Cyrillic map -- and why a browser refresh cleaned it up.
//
// diff:false matters. The default diffs the incoming style against the live one, and the
// style fetched from /basemap/ still carries the original text-field, so a diffing reload
// would just replay setLayoutProperty and reproduce the same partial update. A full
// rebuild drops every parsed tile and re-lays them out with the current expression.
//
// style.load re-adds the heat layers and re-applies both label state and language, so
// nothing else needs restoring here. Node markers are DOM and are untouched.
function reloadStyleForLabels() {
  if (!_labelLangReady || RASTER) return;
  const url = TILESETS[currentTileset] && TILESETS[currentTileset].style;
  if (!url) return;
  try {
    styleReady = false;
    map.setStyle(url, { diff: false });
  } catch (e) {
    console.warn('style reload for label language failed:', e);
    applyLabelLanguage();   // at least update what is already on screen
  }
}

function setLabelLang(code) {
  if (!code) return;
  const hit = _byLower.get(String(code).toLowerCase())
           || _byLower.get(String(code).toLowerCase().split('-')[0]);
  if (!hit || hit === _nameLang) return;
  _nameLang = hit;
  NAME_EXPR = nameExprFor(hit);
  _NAME_EXPR_JSON = JSON.stringify(NAME_EXPR);
  localStorage.setItem('label-lang', hit);
  applyLabelLanguage();
  reloadStyleForLabels();
}

function applyLabelLanguage() {
  let layers;
  try { layers = map.getStyle().layers || []; } catch (e) { return; }
  for (const l of layers) {
    if (l.type !== 'symbol') continue;
    const tf = (l.layout || {})['text-field'];
    if (tf === undefined) continue;
    // Only rewrite fields that render a name. Road shields ({ref}) and house numbers
    // ({housenumber}) are symbol layers too, and replacing those would blank them.
    const cur = JSON.stringify(tf);
    if (!cur.includes('name')) continue;
    if (cur === _NAME_EXPR_JSON) continue;      // already ours; keeps idle cheap
    try { map.setLayoutProperty(l.id, 'text-field', NAME_EXPR); } catch (e) { /* mid-swap */ }
  }
}

// setStyle replaces the layer list wholesale and can settle after style.load has already
// run, so re-check whenever the map goes quiet. Cheap, because both passes only write
// when a layer actually disagrees.
map.on('idle', () => { applyGeoLabels(); applyLabelLanguage(); });

_labelBtn.addEventListener('click', e => {
  e.preventDefault();
  e.stopPropagation();
  _geoLabelsOn = !_geoLabelsOn;
  applyGeoLabels();
});
applyGeoLabels();

// ---- Marker size scale ----
const MARKER_SCALES = { small: 1.0, medium: 1.25, large: 1.5, xl: 1.75 };
let markerScale = MARKER_SCALES[localStorage.getItem('marker-size') || 'small'];

function _sr(base) { return Math.round(base * markerScale); }

function applyMarkerSize() {
  let styleEl = document.getElementById('size-style');
  if (!styleEl) { styleEl = document.createElement('style'); styleEl.id = 'size-style'; document.head.appendChild(styleEl); }
  styleEl.textContent = `.node-label { font-size: ${_sr(7)}px !important; }`;
}

const sizeSel = document.getElementById('size-select');
sizeSel.value = localStorage.getItem('marker-size') || 'small';
sizeSel.addEventListener('change', () => {
  markerScale = MARKER_SCALES[sizeSel.value];
  localStorage.setItem('marker-size', sizeSel.value);
  applyMarkerSize();
  renderDiscovery();
});
applyMarkerSize();

// ---- Infrastructure discovery layer ----
const TYPE_COLORS = { RNode: '#f80', Backbone: '#48f', I2P: '#ad4', HaLow: '#0cf', Yggdrasil: '#b45cff' };

// Nodes are DOM markers (maplibregl.Marker with a custom element) rather than native
// GL circle/symbol layers. That keeps the .node-label chrome and the expand/collapse
// animation working as they did under Leaflet, and a few hundred markers sit well
// inside what the DOM handles comfortably. Native layers would win on label collision
// handling, at the cost of rebuilding all of the above.

function labelsOn() { return document.getElementById('check-labels').checked; }

// The label element is kept on the marker so it can be shown/hidden without a re-render
// (Leaflet's bindTooltip/openTooltip/closeTooltip did this bookkeeping for us). Named
// without an underscore so it cannot collide with MapLibre's own fields.
function _setLabelVisible(marker, visible) {
  if (marker.wdLabel) marker.wdLabel.classList.toggle('node-label-hidden', !visible);
}

// L.circleMarker drew stroke and fill at separate opacities; reproduce that with two
// rgba values rather than a single element opacity.
function _rgba(hex, a) {
  let h = hex.slice(1);
  if (h.length === 3) h = h.split('').map(c => c + c).join('');
  const n = parseInt(h, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

// HaLow nodes report an all-zero address when none is configured.
function _hasLxmf(n) { return !!n.lxmf && n.lxmf !== '0'.repeat(32); }

function makeNodeElement(node, labelPos) {
  const color = node.gateway ? '#38bdf8' : (TYPE_COLORS[node.type] || '#888');
  const stale = node.last_heard && (Date.now() / 1000 - node.last_heard) > 172800;
  const d     = _sr(node.gateway ? 15 : 10);
  const el  = document.createElement('div');
  el.className = 'node-marker';
  const dot = document.createElement('div');
  dot.className = 'node-dot';
  dot.style.cssText =
    `width:${d}px;height:${d}px;` +
    `background:${_rgba(color, stale ? 0.25 : 0.75)};` +
    `border:${node.gateway ? '2px' : '1px'} solid ${_rgba(color, stale ? 0.4 : 0.95)};`;
  const lbl = document.createElement('div');
  lbl.className = `node-label ${node.gateway ? 'node-label-gateway' : 'node-label-' + node.type.toLowerCase()} ${labelPos}`;
  lbl.textContent = node.name;
  el.appendChild(dot);
  el.appendChild(lbl);
  return { el, lbl };
}

// Distance from a marker's centre to the popup tip: half the dot, plus the tip itself.
function _popupGap() { return Math.round(_sr(10) / 2) + 2; }

function makePopup(n, opts) {
  return new maplibregl.Popup(Object.assign({
    className: 'map-popup',
    maxWidth:  '420px',
    offset:    _popupGap(),
  }, opts || {})).setHTML(`<div class="popup-body">${buildPopupLines(n).join('<br>')}</div>`);
}

// Point a cluster member's popup at the member rather than at the cluster.
//
// Every marker in a cluster shares one lngLat and is separated only by a pixel offset, so
// their popups would otherwise all open in the same place -- over the expanded column,
// covering the very nodes being chosen between. Expanded members therefore get an explicit
// popup offset matching their position in the column and anchor:'right', which puts the
// box to the left of the column with its tip on the right edge, aimed at the node.
//
// anchor is a constructor option, so the popup is rebuilt rather than mutated; that keeps
// this on MapLibre's public API instead of reaching into popup.options.
function _retargetPopup(group, marker, i) {
  const existing = marker.getPopup();
  if (existing && existing.isOpen()) existing.remove();
  const opts = group.expanded
    ? { anchor: 'right', offset: [-_popupGap(), expandedOffset(i, group.nodes.length)[1]] }
    : {};
  marker.setPopup(makePopup(group.nodes[i], opts));
  _bindPopupLabel(marker, () => _labelWanted(group, i));
}

// Hide the permanent label while the popup covers it. whenClosed decides what to
// restore to: single markers follow the Labels checkbox, expanded stack members are
// always labelled.
function _bindPopupLabel(marker, whenClosed) {
  const popup = marker.getPopup();
  if (!popup) return;
  popup.on('open',  () => _setLabelVisible(marker, false));
  popup.on('close', () => _setLabelVisible(marker, whenClosed()));
}

// Yggdrasil peers announce as BackboneInterface but are reachable on the
// 200::/7 address range; split them into their own type/color for the map.
function _isYggdrasilRemote(remote) {
  return typeof remote === 'string' && /^[23]0[0-6]:/i.test(remote);
}

function fmtUptime(s) {
  if (!s) return '';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h';
  return Math.floor(s / 86400) + 'd ' + Math.floor((s % 86400) / 3600) + 'h';
}
function fmtHz(hz) {
  if (!hz) return '';
  return (hz / 1e6).toFixed(3) + ' MHz';
}
function fmtBw(hz) {
  if (!hz) return '';
  return hz >= 1000 ? (hz / 1000).toFixed(1) + ' kHz' : hz + ' Hz';
}
function fmtAge(ts) {
  if (!ts) return t('misc.unknown', 'unknown');
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 120)    return s + ' ' + t('age.s', 's ago');
  if (s < 7200)   return Math.floor(s / 60) + ' ' + t('age.m', 'm ago');
  if (s < 172800) return Math.floor(s / 3600) + ' ' + t('age.h', 'h ago');
  return Math.floor(s / 86400) + ' ' + t('age.d', 'd ago');
}

function buildPopupLines(n) {
  // A class rather than an inline colour, so the light-theme rules can reach it -- the
  // popup title had the same low contrast on a pale background as the captions did.
  const typeCls = n.gateway ? 'node-type-gateway' : (TYPE_COLORS[n.type] ? `node-type-${n.type.toLowerCase()}` : 'node-type-unknown');
  const lines = [
    `<b class="popup-title ${typeCls}">${n.name}</b>`,
    `<span style="color:#666">${n.gateway ? t('pop.gateway', 'Gateway') : n.type}</span>`,
  ];
  if (n.last_heard) lines.push(`${t('pop.heard', 'heard')}: ${fmtAge(n.last_heard)}`);
  if (n.frequency) lines.push(`${t('pop.freq', 'freq')}: ${fmtHz(n.frequency)}`);
  if (n.bandwidth) { let l = `${t('pop.bw', 'bw')}: ${fmtBw(n.bandwidth)}`; if (n.sf) l += `  SF${n.sf}`; if (n.cr) l += ` CR${n.cr}`; lines.push(l); }
  if (n.height && n.type !== 'Backbone') lines.push(`${t('pop.alt', 'alt')}: ${Math.round(n.height)}m`);
  if (n.remote) lines.push(`${t('pop.remote', 'remote')}: ${n.remote}:${n.port}`);
  if (n.b32)    lines.push(`${t('pop.remote', 'remote')}: <span style="color:#666">${n.b32}</span>`);
  if (n.hash)   lines.push(`${t('pop.hash', 'hash')}: <span style="color:#666">${n.hash}</span>`);
  if (n.freq_str) lines.push(`${t('pop.freq', 'freq')}: ${n.freq_str}`);
  if (n.bw_str)   lines.push(`${t('pop.bw', 'bw')}: ${n.bw_str}`);
  if (n.fw)       lines.push(`${t('pop.fw', 'fw')}: ${n.fw}`);
  if (n.uptime)   lines.push(`${t('pop.up', 'up')}: ${fmtUptime(n.uptime)}`);
  if (n.version)  lines.push(`${t('pop.ver', 'version')}: ${n.version}`);
  if (_hasLxmf(n)) lines.push(`lxmf: <span style="color:#666">${n.lxmf}</span>`);
  if (n.tx_p != null) lines.push(`${t('pop.tx', 'tx')}: ${n.tx_p} ${t('pop.pkts', 'pkts')}  ${t('pop.rx', 'rx')}: ${n.rx_p} ${t('pop.pkts', 'pkts')}`);
  return lines;
}

let discoveryCache = [];

// --- Node clusters ---
// Nodes closer together than a marker's width are drawn as one cluster: a marker per
// node, all pinned to the same coordinate and separated by a *pixel* offset. Collapsed,
// the offsets fan the dots into the stacked row; expanded, the same markers animate into
// a vertical column. Clicking repositions the existing markers rather than overlaying a
// second set on a dimmed copy of the first, so nothing is left sitting behind.
//
// Keeping the layout in pixel space also makes it zoom-independent -- offsets need no
// recomputation when the map scales -- and a lone node is just a cluster of one at [0,0],
// so single and stacked nodes no longer need separate rendering paths.
const clusterGroups = [];
let expandedCluster = null;

const EXPAND_SPREAD_PX = 22;
const EXPAND_MS   = 220;
const COLLAPSE_MS = 160;
let _animFrame = null;

function _ease(t) { return t < 0.5 ? 2*t*t : -1+(4-2*t)*t; }

function allMarkers() {
  return clusterGroups.flatMap(g => g.markers);
}
function clearMarkers() {
  allMarkers().forEach(m => m.remove());
  clusterGroups.length = 0;
  expandedCluster = null;
}

// Offsets in CSS pixels from the cluster's shared map coordinate. The collapsed row is
// centred so the stack sits over the coordinate rather than starting at it.
function collapsedOffset(i, n) {
  const step = _sr(4), d = _sr(10);
  const totalW = d + (n - 1) * step;
  return [i * step - totalW / 2 + d / 2, 0];
}
function expandedOffset(i, n) {
  return [0, (i - (n - 1) / 2) * EXPAND_SPREAD_PX];
}

// Track the plain array we set so the animation can interpolate without converting on
// every frame -- getOffset() hands back MapLibre's own Point type.
//
// Deliberately NOT stored on marker._offset: that is Marker's own field, holding a Point
// that _update() adds to the projected position on every map move. Overwriting it with
// an array made that arithmetic produce NaN, and markers stopped following the map until
// a zoom rebuilt them. Custom state on a library object goes in an unprefixed name.
function setMarkerOffset(marker, off) {
  marker.wdOffset = off;
  marker.setOffset(off);
}

function _animateOffsets(markers, from, to, duration, onComplete) {
  if (_animFrame) { cancelAnimationFrame(_animFrame); _animFrame = null; }
  const t0 = performance.now();
  function frame(now) {
    const t = Math.min(1, (now - t0) / duration);
    const e = _ease(t);
    markers.forEach((m, i) => setMarkerOffset(m, [
      from[i][0] + (to[i][0] - from[i][0]) * e,
      from[i][1] + (to[i][1] - from[i][1]) * e,
    ]));
    if (t < 1) { _animFrame = requestAnimationFrame(frame); }
    else { _animFrame = null; if (onComplete) onComplete(); }
  }
  _animFrame = requestAnimationFrame(frame);
}

// Which member names the cluster while collapsed -- infrastructure types win over
// whatever happens to sort first, as the previous stack label did.
function clusterLabelIndex(nodes) {
  for (const type of ['Backbone', 'Yggdrasil', 'HaLow']) {
    const i = nodes.findIndex(n => n.type === type);
    if (i >= 0) return i;
  }
  return 0;
}

function _setLabelSide(marker, node, side) {
  marker.wdLabel.className = `node-label node-label-${node.type.toLowerCase()} ${side}`;
}

// Collapsed, only the naming member is labelled -- N captions stacked on one point would
// be an unreadable pile. Expanded, every member is labelled beside its own dot.
function _labelWanted(group, i) {
  return labelsOn() && (group.expanded || i === group.labelIndex);
}
function applyClusterLabels(group) {
  group.markers.forEach((m, i) => {
    _setLabelSide(m, group.nodes[i], group.expanded ? 'node-label-right' : 'node-label-top');
    _setLabelVisible(m, _labelWanted(group, i));
  });
}

function expandCluster(group) {
  if (expandedCluster && expandedCluster !== group) collapseCluster(expandedCluster);
  if (group.expanded || group.nodes.length < 2) return;
  group.expanded = true;
  expandedCluster = group;
  const n = group.nodes.length;
  group.markers.forEach((m, i) => _retargetPopup(group, m, i));
  group.markers.forEach(m => _setLabelVisible(m, false));   // hidden while travelling
  _animateOffsets(group.markers,
    group.markers.map(m => m.wdOffset),
    group.markers.map((_, i) => expandedOffset(i, n)),
    EXPAND_MS, () => applyClusterLabels(group));
}

function collapseCluster(group) {
  if (!group || !group.expanded) return;
  group.expanded = false;
  if (expandedCluster === group) expandedCluster = null;
  const n = group.nodes.length;
  group.markers.forEach((m, i) => {
    _setLabelVisible(m, false);
    _retargetPopup(group, m, i);   // closes an open popup and restores the default anchor
  });
  _animateOffsets(group.markers,
    group.markers.map(m => m.wdOffset),
    group.markers.map((_, i) => collapsedOffset(i, n)),
    COLLAPSE_MS, () => applyClusterLabels(group));
}

function collapseGroup() { collapseCluster(expandedCluster); }

function addCluster(nodes) {
  const n = nodes.length;
  const group = { nodes, markers: [], expanded: false, labelIndex: clusterLabelIndex(nodes) };
  nodes.forEach((node, i) => {
    const { el, lbl } = makeNodeElement(node, 'node-label-top');
    const off = collapsedOffset(i, n);
    const marker = new maplibregl.Marker({ element: el, offset: off })
      .setLngLat([nodes[0].lon, nodes[0].lat])
      .setPopup(makePopup(node))
      .addTo(map);
    marker.wdLabel  = lbl;
    marker.wdOffset = off;
    _bindPopupLabel(marker, () => _labelWanted(group, i));
    el.addEventListener('click', (e) => {
      // Markers live inside the canvas container, so without this the map-level collapse
      // handler runs on the same click and undoes whatever happens here.
      e.stopPropagation();
      if (expandedCluster && expandedCluster !== group) collapseCluster(expandedCluster);
      if (!group.expanded && n > 1) { expandCluster(group); return; }
      // MapLibre toggles marker popups from that same suppressed map-level listener, so
      // do it directly, keeping one popup open at a time as the Leaflet popups did.
      group.markers.forEach(m => {
        const p = m.getPopup();
        if (m !== marker && p && p.isOpen()) p.remove();
      });
      marker.togglePopup();
    });
    group.markers.push(marker);
  });
  applyClusterLabels(group);
  clusterGroups.push(group);
}

// Cluster membership is a pixel-distance test, so it is recomputed when the map scales --
// except while a cluster is expanded, where a re-render would throw the expansion away.
// The expanded layout itself needs no recomputation; its offsets are already in pixels.
map.on('zoomend', () => { if (!expandedCluster) renderDiscovery(); });

map.on('click', () => collapseGroup());

function renderDiscovery() {
  clearMarkers();

  const visibleTypes = new Set();
  if (document.getElementById('check-rnode').checked)    visibleTypes.add('RNode');
  if (document.getElementById('check-backbone').checked) visibleTypes.add('Backbone');
  if (document.getElementById('check-i2p').checked)      visibleTypes.add('I2P');
  if (document.getElementById('check-halow').checked)    visibleTypes.add('HaLow');
  if (document.getElementById('check-ygg').checked)      visibleTypes.add('Yggdrasil');

  const filtered = discoveryCache.filter(n => n.gateway || (visibleTypes.has(n.type) && _geoFilterMatches(n)));
  const threshold = _sr(10);
  const groups = [];
  filtered.forEach(n => {
    const pt = map.project([n.lon, n.lat]);
    const hit = groups.find(c => {
      const cp = map.project([c[0].lon, c[0].lat]);
      return Math.hypot(pt.x - cp.x, pt.y - cp.y) < threshold;
    });
    if (hit) hit.push(n);
    else groups.push([n]);
  });

  groups.forEach(addCluster);
}


function renderNodesList() {
  const list = document.getElementById('nodes-list');
  if (!list || document.getElementById('nodes-panel').style.display === 'none') return;
  const visibleTypes = new Set();
  if (document.getElementById('check-rnode').checked)    visibleTypes.add('RNode');
  if (document.getElementById('check-backbone').checked) visibleTypes.add('Backbone');
  if (document.getElementById('check-i2p').checked)      visibleTypes.add('I2P');
  if (document.getElementById('check-halow').checked)    visibleTypes.add('HaLow');
  if (document.getElementById('check-ygg').checked)      visibleTypes.add('Yggdrasil');
  const query = document.getElementById('nodes-search').value.toLowerCase().trim();
  const geoC  = document.getElementById('geo-country').value;
  const geoS  = document.getElementById('geo-state').value;
  const geoCi = document.getElementById('geo-city').value;
  const nodes = discoveryCache.filter(n =>
    visibleTypes.has(n.type) &&
    (!query  || n.name.toLowerCase().includes(query) || (_hasLxmf(n) && n.lxmf.includes(query))) &&
    (!geoC   || n.country === geoC)  &&
    (!geoS   || n.state   === geoS)  &&
    (!geoCi  || n.city    === geoCi)
  );
  const totalEl = document.getElementById('nodes-total');
  // Assigned before the early return below: a filter that matches nothing still has to
  // report zero rather than leave the previous count on screen.
  if (totalEl) totalEl.textContent = nodes.length;

  list.innerHTML = '';
  if (nodes.length === 0) {
    list.innerHTML = `<div style="padding:12px;font-size:11px;color:var(--text-muted)">${t('nodes.empty', 'No nodes')}</div>`;
    return;
  }
  nodes.forEach(n => {
    const color = TYPE_COLORS[n.type] || '#888';
    const div = document.createElement('div');
    div.className = 'node-entry';
    const parts = [];
    if (n.frequency) parts.push(fmtHz(n.frequency));
    if (n.bandwidth) { let s = fmtBw(n.bandwidth); if (n.sf) s += `  SF${n.sf}`; if (n.cr) s += ` CR${n.cr}`; parts.push(s); }
    if (n.remote) parts.push(`${n.remote}:${n.port}`);
    if (n.b32) parts.push(n.b32.length > 24 ? n.b32.substring(0, 22) + '…' : n.b32);
    if (n.freq_str) parts.push(n.freq_str);
    if (n.bw_str)   parts.push(n.bw_str);
    if (n.fw)       parts.push(`fw ${n.fw}`);
    if (_hasLxmf(n)) parts.push(`lxmf ${n.lxmf}`);
    const age = n.last_heard ? fmtAge(n.last_heard) : '';
    const dot = `<span style="width:8px;height:8px;border-radius:50%;background:${color};flex-shrink:0;display:inline-block"></span>`;
    div.innerHTML =
      `<div class="node-entry-name"><span class="node-entry-name-left">${dot}<span>${n.name}</span></span><span class="node-entry-type">${n.type}</span></div>` +
      (parts.length ? `<div class="node-entry-meta">${parts.join('  ')}</div>` : '') +
      `<div class="node-entry-bottom"><span class="node-entry-coords">${n.lat.toFixed(5)}, ${n.lon.toFixed(5)}</span>${age ? `<span class="node-entry-age">${age}</span>` : ''}</div>`;
    div.addEventListener('click', () => focusOn(n.lat, n.lon, 15));
    list.appendChild(div);
  });
}

document.getElementById('nodes-search').addEventListener('input', renderNodesList);

let _geoTimer = null;

function _clientGeoKey(lat, lon) {
  return (Math.round(lat * 100) / 100).toFixed(2) + ',' + (Math.round(lon * 100) / 100).toFixed(2);
}

// `label` lets an option display one string and filter on another. The value stays the
// canonical English name that the node records carry, so localising the list cannot
// change what a saved filter matches -- only how it reads.
function _setSelectOptions(sel, values, placeholder, label) {
  const current = sel.value;
  sel.innerHTML = '<option value=""></option>';
  sel.firstChild.textContent = placeholder;
  values.forEach(v => {
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = label ? label(v) : v;
    if (v === current) opt.selected = true;
    sel.appendChild(opt);
  });
  if (current && !values.includes(current)) sel.value = '';
  // Options are sorted on the English name; re-sort on what is actually displayed.
  const opts = [...sel.options].slice(1).sort((a, b) =>
    a.textContent.localeCompare(b.textContent, uiLang));
  opts.forEach(o => sel.appendChild(o));
}

// '<lang>|<English name>' -> name in that language, for regions and cities. Countries
// come from Intl.DisplayNames instead, which needs no data of its own.
const _localPlace = new Map();
function localisePlace(name) {
  return _localPlace.get(uiLang + '|' + name) || name;
}

// English country name -> ISO code, built from the node records the server enriches.
// Intl.DisplayNames needs the code; the geo database now carries it alongside the name.
const _ccByCountry = new Map();

// Falls back to the English name whenever the browser cannot name the code, and for
// records cached before the code was stored.
let _regionNames = null;
function localiseCountry(name) {
  const cc = _ccByCountry.get(name);
  if (!cc) return name;
  try {
    if (!_regionNames || _regionNames._lang !== uiLang) {
      _regionNames = new Intl.DisplayNames([uiLang], { type: 'region' });
      _regionNames._lang = uiLang;
    }
    return _regionNames.of(cc) || name;
  } catch (e) {
    return name;
  }
}

function _geoFilterMatches(n) {
  const c  = document.getElementById('geo-country').value;
  const s  = document.getElementById('geo-state').value;
  const ci = document.getElementById('geo-city').value;
  return (!c  || n.country === c)  &&
         (!s  || n.state   === s)  &&
         (!ci || n.city    === ci);
}

function buildGeoDropdowns() {
  const visibleTypes = new Set();
  if (document.getElementById('check-rnode').checked)    visibleTypes.add('RNode');
  if (document.getElementById('check-backbone').checked) visibleTypes.add('Backbone');
  if (document.getElementById('check-i2p').checked)      visibleTypes.add('I2P');
  if (document.getElementById('check-halow').checked)    visibleTypes.add('HaLow');
  if (document.getElementById('check-ygg').checked)      visibleTypes.add('Yggdrasil');
  const all = discoveryCache.filter(n => visibleTypes.has(n.type));
  const selC  = document.getElementById('geo-country');
  const selS  = document.getElementById('geo-state');
  const selCi = document.getElementById('geo-city');
  if (all.length > 0 && all.every(n => !n.country)) {
    selC.innerHTML  = '<option value=""></option>';
    selC.firstChild.textContent = t('geo.geocoding', 'Geocoding…');
    selS.innerHTML  = '<option value="">—</option>';
    selCi.innerHTML = '<option value="">—</option>';
    return;
  }
  all.forEach(n => {
    if (n.country && n.cc) _ccByCountry.set(n.country, n.cc);
    // Local names are keyed by the English value the filter matches on, and only kept
    // when they are in a language we might display.
    if (n.loc_lang) {
      if (n.city  && n.city_loc)  _localPlace.set(n.loc_lang + '|' + n.city,  n.city_loc);
      if (n.state && n.state_loc) _localPlace.set(n.loc_lang + '|' + n.state, n.state_loc);
    }
  });
  const countries = [...new Set(all.filter(n => n.country).map(n => n.country))].sort();
  _setSelectOptions(selC, countries, t('geo.allCountries', 'All Countries'), localiseCountry);
  if (!selC.value) {
    const def = localStorage.getItem('geo-filter-country') ?? DEFAULT_COUNTRY;
    if (countries.includes(def)) selC.value = def;
  }
  const byCountry = selC.value ? all.filter(n => n.country === selC.value) : all;
  const states = [...new Set(byCountry.filter(n => n.state).map(n => n.state))].sort();
  _setSelectOptions(selS, states, t('geo.allRegions', 'All Regions'), localisePlace);
  if (!selS.value) {
    const def = localStorage.getItem('geo-filter-state') ?? '';
    if (states.includes(def)) selS.value = def;
  }
  const byState = selS.value ? byCountry.filter(n => n.state === selS.value) : byCountry;
  const cities = [...new Set(byState.filter(n => n.city).map(n => n.city))].sort();
  _setSelectOptions(selCi, cities, t('geo.allCities', 'All Cities'), localisePlace);
  if (!selCi.value) {
    const def = localStorage.getItem('geo-filter-city') ?? '';
    if (cities.includes(def)) selCi.value = def;
  }
}

function updateGeoData() {
  fetch('/api/geo').then(r => r.json()).then(cache => {
    let changed = false;
    discoveryCache.forEach(n => {
      if (n.country) return;
      const g = cache[_clientGeoKey(n.lat, n.lon)];
      if (g && (g.country || g.city)) {
        n.country = g.country; n.state = g.state; n.city = g.city;
        changed = true;
      }
    });
    if (changed) { buildGeoDropdowns(); renderNodesList(); }
    if (!discoveryCache.some(n => !n.country)) { clearInterval(_geoTimer); _geoTimer = null; }
  }).catch(() => {});
}

document.getElementById('geo-country').addEventListener('change', () => {
  localStorage.setItem('geo-filter-country', document.getElementById('geo-country').value);
  localStorage.removeItem('geo-filter-state');
  localStorage.removeItem('geo-filter-city');
  document.getElementById('geo-state').value = '';
  document.getElementById('geo-city').value  = '';
  buildGeoDropdowns();
  renderDiscovery();
  renderNodesList();
});
document.getElementById('geo-state').addEventListener('change', () => {
  localStorage.setItem('geo-filter-state', document.getElementById('geo-state').value);
  localStorage.removeItem('geo-filter-city');
  document.getElementById('geo-city').value = '';
  buildGeoDropdowns();
  renderDiscovery();
  renderNodesList();
});
document.getElementById('geo-city').addEventListener('change', () => {
  localStorage.setItem('geo-filter-city', document.getElementById('geo-city').value);
  renderDiscovery();
  renderNodesList();
});

document.getElementById('nodes-close').addEventListener('click', () => {
  document.getElementById('nodes-panel').style.display = 'none';
  document.getElementById('nodes-open').classList.remove('active');
  if (_geoTimer) { clearInterval(_geoTimer); _geoTimer = null; }
});
document.getElementById('nodes-open').addEventListener('click', () => {
  const panel = document.getElementById('nodes-panel');
  const open = panel.style.display !== 'flex';
  panel.style.display = open ? 'flex' : 'none';
  document.getElementById('panel').style.display = 'none';
  document.getElementById('nodes-open').classList.toggle('active', open);
  document.getElementById('panel-open').classList.remove('active');
  if (!open && _geoTimer) { clearInterval(_geoTimer); _geoTimer = null; }
  if (!open) return;
  buildGeoDropdowns();
  renderNodesList();
  if (discoveryCache.some(n => !n.country)) {
    updateGeoData();
    _geoTimer = setInterval(updateGeoData, 5000);
  }
});

function setDiscoveryWarning(show) {
  document.getElementById('discovery-warning').style.display = show ? 'block' : 'none';
}
function loadDiscovered() {
  fetch('/api/discovered').then(r => {
    setDiscoveryWarning(r.headers.get('X-Rnstatus-OK') === '0');
    return r.json();
  }).then(nodes => {
    nodes.forEach(n => {
      if (n.type === 'Backbone' && _isYggdrasilRemote(n.remote)) n.type = 'Yggdrasil';
    });
    discoveryCache = nodes;
    buildGeoDropdowns();
    renderDiscovery();
    renderNodesList();
  }).catch(() => { setDiscoveryWarning(true); });
}

const LAYER_CHECKS = {
  'check-rnode':    { dot: 'tab-dot-rnode'    },
  'check-backbone': { dot: 'tab-dot-backbone' },
  'check-i2p':      { dot: 'tab-dot-i2p'     },
  'check-halow':    { dot: 'tab-dot-halow'   },
  'check-ygg':      { dot: 'tab-dot-ygg'     },
};
function syncTabDot(id, checked) {
  const dot = document.getElementById(LAYER_CHECKS[id].dot);
  if (dot) dot.style.display = checked ? 'inline-block' : 'none';
}

// Restore persisted checkbox states before init
[...Object.keys(LAYER_CHECKS), 'check-labels'].forEach(id => {
  const saved = localStorage.getItem(id);
  if (saved !== null) document.getElementById(id).checked = saved === 'true';
});

Object.entries(LAYER_CHECKS).forEach(([id]) => {
  syncTabDot(id, document.getElementById(id).checked);
  document.getElementById(id).addEventListener('change', e => {
    localStorage.setItem(id, e.target.checked);
    syncTabDot(id, e.target.checked);
    renderDiscovery();
    renderNodesList();
    buildGeoDropdowns();
  });
});

document.getElementById('check-labels').addEventListener('change', e => {
  localStorage.setItem('check-labels', e.target.checked);
  clusterGroups.forEach(applyClusterLabels);
});

loadDiscovered();
setInterval(loadDiscovered, 300000); // refresh every 5 minutes

// ---- Theme toggle ----
function applyTheme(light) {
  document.body.classList.toggle('light-theme', light);
  try { applyRuler(); applyQuickRuler(); } catch (e) {}   // the rulers' colour follows the theme
}
// The basemap is authoritative for chrome on load, not the stored theme. Both the
// toggle and the selector always write the two together, so they only diverge from a
// stale localStorage -- which is exactly the state anyone who picked a dark basemap
// before it carried a `dark` flag is currently stuck in.
//
// Without a dark/light pair of basemaps the two are independent, and the stored theme
// is the only record of what the visitor chose.
if (THEME_PAIRED) {
  applyTheme(!TILESETS[currentTileset].dark);
  localStorage.setItem('theme', TILESETS[currentTileset].dark ? 'dark' : 'light');
} else {
  applyTheme(localStorage.getItem('theme') === 'light');
}
function applyThemeTileset(newLight) {
  if (!THEME_PAIRED) return;
  const idx = newLight ? LIGHT_TILESET : DARK_TILESET;
  const sel = document.getElementById('tileset-select');
  if (sel) sel.value = idx;
  setBasemap(idx);
  localStorage.setItem('tileset', TILESETS[idx].id);
}

window.addEventListener('message', function(e) {
  if (e.source !== window.parent || window.parent === window) return;
  // The parent sends the theme as a bare string; language arrives as an object so the
  // two cannot be confused, and so older parents keep working unchanged.
  if (e.data && typeof e.data === 'object' && e.data.type === 'lang') {
    setUiLang(e.data.lang, true);
    return;
  }
  if (e.data === 'light' || e.data === 'dark') {
    const newLight = e.data === 'light';
    applyTheme(newLight);
    applyThemeTileset(newLight);
    localStorage.setItem('theme', e.data);
  }
});
document.getElementById('theme-toggle').addEventListener('click', () => {
  const newLight = !document.body.classList.contains('light-theme');
  localStorage.setItem('theme', newLight ? 'light' : 'dark');
  applyTheme(newLight);
  applyThemeTileset(newLight);
});

// ---- Language toggle ----
// Standalone only. Embedded, the parent page drives the language over postMessage and
// shows its own RU/EN switch, so exposing a second one here would let the two disagree.
const langBtn = document.getElementById('lang-toggle');
if (langBtn && !EMBEDDED) {
  langBtn.style.display = 'flex';
  langBtn.addEventListener('click', e => {
    e.preventDefault();
    e.stopPropagation();
    setUiLang(uiLang === 'ru' ? 'en' : 'ru', true);
  });
}
// Runs last so every dictionary, control and render function it touches exists. Embedded,
// this still applies a sensible default until the parent's message arrives.
applyI18n();
setLabelLang(uiLang);
// Only now may a language change rebuild the style; the initial call above must not.
_labelLangReady = true;
refreshLocalisedViews();

// ---- Tileset selector ----
function setBasemap(idx) {
  if (idx === currentTileset || !TILESETS[idx]) return;
  currentTileset = idx;
  styleReady = false;          // 'style.load' re-adds the heat layers and sets it back
  // diff:false is what makes that event fire. The default diffs the new style against the
  // live one, which removes the heat/ruler layers (they are not in the incoming style) and,
  // when the diff applies cleanly, never fires style.load -- so they stayed gone.
  map.setStyle(TILESETS[idx].style, { diff: false });
}

const tilesetSel = document.getElementById('tileset-select');
TILESETS.forEach((tileset, i) => {
  const opt = document.createElement('option');
  opt.value = i;
  if (tileset.labelKey) opt.setAttribute('data-i18n', tileset.labelKey);
  opt.textContent = tileset.labelKey ? t(tileset.labelKey, tileset.label) : tileset.label;
  tilesetSel.appendChild(opt);
});
// A one-entry dropdown is just noise; it reappears as soon as a second style is added.
if (TILESETS.length < 2) tilesetSel.style.display = 'none';

tilesetSel.value = currentTileset;
tilesetSel.addEventListener('change', () => {
  const idx = parseInt(tilesetSel.value, 10);
  localStorage.setItem('tileset', TILESETS[idx].id);
  setBasemap(idx);
  // Chrome follows the basemap's own `dark` flag, so a dark basemap keeps dark
  // buttons and tabs no matter where it sits in the list.
  const newLight = !TILESETS[idx].dark;
  localStorage.setItem('theme', newLight ? 'light' : 'dark');
  applyTheme(newLight);
});
</script>
</body>
</html>"""


def _db_present():
    """The bot database is optional (the map can just view exported files). Checked before
    every connect because sqlite3.connect() would otherwise create an empty file there."""
    return os.path.exists(DB_PATH)


def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _gateway_position():
    """Read the gateway origin configured in NOEMA Dashboard → System → Globe."""
    try:
        with open(GATEWAY_POSITION_PATH, encoding="utf-8") as fh:
            origin = json.load(fh).get("origin") or {}
        lat, lon = float(origin["lat"]), float(origin["lon"])
        if not (math.isfinite(lat) and math.isfinite(lon)
                and -90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return {"lat": lat, "lon": lon, "label": str(origin.get("label") or "").strip()[:40]}
    except (OSError, ValueError, TypeError, KeyError):
        return None


@app.route("/")
def index():
    has_initial = all(x is not None for x in [INITIAL_LAT, INITIAL_LON, INITIAL_ZOOM])
    gateway = _gateway_position()
    initial_lat = gateway["lat"] if gateway else (INITIAL_LAT if has_initial else 0)
    initial_lon = gateway["lon"] if gateway else (INITIAL_LON if has_initial else 0)
    initial_zoom = 11 if gateway else (INITIAL_ZOOM if has_initial else 2)
    resp = make_response(render_template_string(MAP_HTML,
        page_title = PAGE_TITLE,
        init_lat   = initial_lat,
        init_lon   = initial_lon,
        init_zoom  = initial_zoom,
        gateway_position = json.dumps(gateway),
        fit_done   = 'true' if (gateway or has_initial) else 'false',
        overlay_on = OVERLAY_ENABLED,
        js_base    = JS_BASE,
        raster     = BASEMAP_IS_RASTER,
        basemap    = json.dumps({"provider": BASEMAP_PROVIDER,
                                 "base": BASEMAP_URL if BASEMAP_PROVIDER == "remote" else "",
                                 "raster": RASTER_TILES}),
        default_country = json.dumps(DEFAULT_COUNTRY)))
    # The page carries all of its own JS inline, so a cached copy silently keeps running
    # the previous build after a deploy. Assets under /static are separately versioned
    # by content and still cache normally.
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp



@app.route("/api/range")
def api_range():
    if not _db_present():
        return jsonify({"min_ts": None, "max_ts": None})
    try:
        con = get_db()
        row = con.execute("SELECT MIN(ts) as min_ts, MAX(ts) as max_ts FROM points").fetchone()
        con.close()
        return jsonify({"min_ts": row["min_ts"], "max_ts": row["max_ts"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/nodes")
def api_nodes():
    if not _db_present():
        return jsonify([])
    try:
        con = get_db()
        rows = con.execute("SELECT DISTINCT sender_hash FROM points").fetchall()
        con.close()
        return jsonify([r["sender_hash"].hex() for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/points")
def api_points():
    since   = request.args.get("since",   0,    type=int)
    from_ts = request.args.get("from_ts", None, type=int)
    to_ts   = request.args.get("to_ts",   None, type=int)
    sender  = "".join(ch for ch in (request.args.get("sender") or "") if ch in "0123456789abcdefABCDEF").lower()
    if not _db_present():
        return jsonify([])
    try:
        con = get_db()
        sender_sql = " AND lower(hex(sender_hash)) = ?" if sender else ""
        sender_args = (sender,) if sender else ()
        if from_ts is not None:
            rows = con.execute(
                "SELECT lat, lon, rssi, snr, dl_rssi, dl_snr, q, ts, sender_hash, source FROM points "
                "WHERE ts >= ? AND ts <= ?" + sender_sql + " ORDER BY ts",
                (from_ts, to_ts if to_ts is not None else int(time.time())) + sender_args,
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT lat, lon, rssi, snr, dl_rssi, dl_snr, q, ts, sender_hash, source FROM points "
                "WHERE ts > ?" + sender_sql + " ORDER BY ts",
                (since,) + sender_args,
            ).fetchall()
        con.close()
        return jsonify([{
            **dict(r),
            "lat": r["lat"] / 1_000_000,
            "lon": r["lon"] / 1_000_000,
            "sender_hash": r["sender_hash"].hex(),
            "source": r["source"],
        } for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# HaLow RNodes publish their own telemetry to an MQTT broker (map.rns.moscow:1883, topic
# rnode-halow/telemetry); this server subscribes and keeps the latest report per node.
# They used to be read from rnode-halow.ru, which is gone. The nodes' credential is
# public, so everything received goes through halow_telemetry.parse_telemetry() before
# it can reach the page. MQTT messages are not retained, so the node table is persisted
# to HALOW_CACHE_PATH and survives a restart; the age of a node shows through the
# existing faded-marker treatment, since each carries its own last_heard timestamp.
#
# "halow_mqtt" in the config: {"host", "port" (1883), "username", "password" or
# "password_file", "topic" ("rnode-halow/telemetry"), "tls" (false)}. Without a host
# there are no HaLow nodes. Needs the paho-mqtt package and halow_telemetry.py beside
# this file; like the overlay, neither is touched unless this is configured.
HALOW_MQTT = _cfg.get("halow_mqtt") or {}
halow_telemetry = None
if HALOW_MQTT.get("host"):
    import halow_telemetry
HALOW_CACHE_PATH = _cfg.get("halow_cache_path",
                            os.path.join(os.path.dirname(DB_PATH), "wardrive_halow_cache.json"))
# Anyone holding the node credential can invent node ids, so the table is bounded.
HALOW_MAX_NODES = 1000
HALOW_SAVE_EVERY_SECS = 60

def _load_halow_cache():
    try:
        with open(HALOW_CACHE_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _save_halow_cache(nodes):
    try:
        tmp = HALOW_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(nodes, f)
        os.replace(tmp, HALOW_CACHE_PATH)
    except Exception as e:
        print(f"[HALOW] cache save failed: {e}", flush=True)

def _halow_fresh(node):
    """False for a node past the same age limit applied to discovered nodes.

    Without this a node that went away for good would sit on the map forever. Entries
    carrying no timestamp are kept, matching how _fetch_rnstatus_nodes treats them -- an
    unknown age is not evidence of staleness. Markers already fade at two days, so a node
    degrades from solid to faded to gone rather than vanishing abruptly.
    """
    lh = node.get("last_heard")
    return lh is None or lh >= time.time() - NODE_PRUNE_AGE

_halow_lock  = threading.Lock()
_halow_nodes = {}      # node_key -> node dict, the latest report from each node
_halow_dirty = False   # changed since the last write to HALOW_CACHE_PATH
_halow_saved = 0.0

if halow_telemetry:
    for _n in _load_halow_cache():
        # Seeded from the file written by an earlier run (or by the old rnode-halow.ru feed,
        # whose entries carry an LXMF address too, so a node is not duplicated by the change).
        if isinstance(_n, dict) and _halow_fresh(_n):
            _halow_nodes[halow_telemetry.node_key(_n.get("lxmf"), None, _n.get("name"))] = _n

def _halow_ingest(payload):
    global _halow_dirty
    parsed = halow_telemetry.parse_telemetry(payload, time.time())
    if parsed is None:
        return
    key, node = parsed
    with _halow_lock:
        if key not in _halow_nodes and len(_halow_nodes) >= HALOW_MAX_NODES:
            for k in [k for k, v in _halow_nodes.items() if not _halow_fresh(v)]:
                del _halow_nodes[k]
            if len(_halow_nodes) >= HALOW_MAX_NODES:
                return
        _halow_nodes[key] = node
        _halow_dirty = True

def _halow_snapshot():
    """The current HaLow node list, written through to the cache file at most once a minute."""
    global _halow_dirty, _halow_saved
    with _halow_lock:
        for k in [k for k, v in _halow_nodes.items() if not _halow_fresh(v)]:
            del _halow_nodes[k]
            _halow_dirty = True
        nodes = [dict(n) for n in _halow_nodes.values()]
        save = _halow_dirty and time.time() - _halow_saved >= HALOW_SAVE_EVERY_SECS
        if save:
            _halow_dirty, _halow_saved = False, time.time()
    if save:
        _save_halow_cache(nodes)
    return nodes

def _start_halow_mqtt():
    cfg = HALOW_MQTT
    if not halow_telemetry:
        return          # halow_mqtt not configured: no HaLow nodes
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("[HALOW] paho-mqtt is not installed; HaLow nodes disabled", flush=True)
        return
    password = cfg.get("password")
    if cfg.get("password_file"):
        try:
            with open(cfg["password_file"]) as f:
                password = f.read().strip()
        except OSError as e:
            print(f"[HALOW] cannot read password_file: {e}", flush=True)
            return
    topic = cfg.get("topic", "rnode-halow/telemetry")
    client_id = f"wardrive-map-{os.getpid()}"
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:      # paho 1.x
        client = mqtt.Client(client_id=client_id)
    if cfg.get("username"):
        client.username_pw_set(cfg["username"], password)
    if cfg.get("tls"):
        client.tls_set()

    def on_connect(c, userdata, flags, rc, *extra):
        # Subscribing here rather than once at startup, so every reconnect resubscribes.
        if getattr(rc, "is_failure", rc not in (0, None)):
            print(f"[HALOW] broker refused the connection: {rc}", flush=True)
            return
        c.subscribe(topic, qos=0)
        print(f"[HALOW] connected to {cfg['host']}, subscribed to {topic}", flush=True)

    def on_disconnect(c, userdata, *args):
        print("[HALOW] broker connection lost; reconnecting", flush=True)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = lambda c, userdata, msg: _halow_ingest(msg.payload)
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    # connect_async + loop_start: a broker that is down at startup is retried in the
    # background instead of stopping the map from coming up.
    client.connect_async(cfg["host"], int(cfg.get("port", 1883)), keepalive=60)
    client.loop_start()

TYPE_LABELS = {
    "RNodeInterface":    "RNode",
    "BackboneInterface": "Backbone",
    "TCPServerInterface":"TCP",
    "TCPClientInterface":"TCP",
    "I2PInterface":      "I2P",
    "UDPInterface":      "UDP",
    "AutoInterface":     "Auto",
}

def _find_rnstatus():
    """Resolve rnstatus: systemd units rarely have the project venv on PATH."""
    import shutil
    home = os.path.expanduser("~")
    configured = _cfg.get("rnstatus_bin") or os.environ.get("RNSTATUS_BIN")
    candidates = []
    if configured:
        candidates.append(os.path.expanduser(str(configured)))
    candidates.extend([
        os.path.join(home, "NOEMA-RNSGate-FULL", ".venv", "bin", "rnstatus"),
        os.path.join(home, "MeshGate", ".venv", "bin", "rnstatus"),
        os.path.join(home, "rns-env", "bin", "rnstatus"),
        "/opt/reticulum/bin/rnstatus",
    ])
    which = shutil.which("rnstatus")
    if which:
        candidates.append(which)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return which or "rnstatus"

_RNSTATUS_BIN = _find_rnstatus()
print(f"[discovery] rnstatus binary: {_RNSTATUS_BIN}", flush=True)

def _fetch_rnstatus_nodes():
    """Query rnstatus for discovered interfaces. Raises on failure/timeout."""
    nodes = []
    result = subprocess.run(
        [_RNSTATUS_BIN, "-d", "-j"],
        capture_output=True, text=True, timeout=10
    )
    raw = json.loads(result.stdout)
    cutoff = time.time() - NODE_PRUNE_AGE
    for d in raw:
        rtype = d.get("type", "")
        if rtype not in ("RNodeInterface", "BackboneInterface", "I2PInterface"):
            continue
        lat = d.get("latitude")
        lon = d.get("longitude")
        if lat is None or lon is None:
            continue
        last_heard = d.get("last_heard")
        if last_heard is not None and last_heard < cutoff:
            continue
        node = {
            "name":        d.get("name", ""),
            "type":        TYPE_LABELS.get(rtype, rtype),
            "type_raw":    rtype,
            "hops":        d.get("hops"),
            "value":       d.get("value"),
            "last_heard":  last_heard,
            "heard_count": d.get("heard_count"),
            "discovered":  d.get("discovered"),
            "status":      d.get("status"),
            "lat":         lat,
            "lon":         lon,
            "height":      d.get("height"),
        }
        # discovery_lxmf_address, the operator's contact address. Checked rather than
        # passed through: it comes off the network and ends up in the page's HTML.
        lxmf = d.get("operator_lxmf_address")
        if isinstance(lxmf, str) and re.fullmatch(r"[0-9a-f]{32}", lxmf):
            node["lxmf"] = lxmf
        # Implementation and version are free-form text chosen by the announcing node
        # ("microReticulum-solar/1"), so the same applies, with a looser pattern.
        impl, ver = d.get("impl_name"), d.get("version")
        if all(isinstance(v, str) and re.fullmatch(r"[\w.+/-]{1,32}", v) for v in (impl, ver)):
            node["version"] = f"{impl} {ver}"
        if rtype == "RNodeInterface":
            node["frequency"]  = d.get("frequency")
            node["bandwidth"]  = d.get("bandwidth")
            node["sf"]         = d.get("sf")
            node["cr"]         = d.get("cr")
        elif rtype == "BackboneInterface":
            node["remote"] = d.get("reachable_on")
            node["port"]   = d.get("port")
        elif rtype == "I2PInterface":
            node["remote"] = d.get("reachable_on")
        nodes.append(node)
    return nodes


# Background discovery cache. rnstatus (subprocess) is a blocking call; running it
# per-request means a wedged rnsd stalls every page load and drops all RNodes. Instead a
# daemon thread refreshes it on an interval and requests serve the last known-good
# snapshot. HaLow nodes arrive by MQTT push, so that refresh just snapshots the table.
_disc_lock  = threading.Lock()
_disc_cache = {
    "rnstatus":    [],    # last successful rnstatus node list
    "rnstatus_ts": 0.0,   # when that list was fetched (0 = never)
    "halow":       [],
}
# rnstatus data older than this is considered unavailable (signals the UI).
DISCOVERY_STALE_SECS = max(60, DISCOVERY_REFRESH_SECS * 4)


def _refresh_discovery():
    while True:
        try:
            nodes = _fetch_rnstatus_nodes()
            with _disc_lock:
                _disc_cache["rnstatus"]    = nodes
                _disc_cache["rnstatus_ts"] = time.time()
        except Exception as e:
            # Keep serving the last good snapshot; api_discovered flags staleness.
            print(f"[WARN] rnstatus refresh failed (serving cached data): {e}", flush=True)
        try:
            halow = _halow_snapshot()
            with _disc_lock:
                _disc_cache["halow"] = halow
        except Exception as e:
            print(f"[WARN] HaLow refresh failed (serving cached data): {e}", flush=True)
        time.sleep(DISCOVERY_REFRESH_SECS)


_start_halow_mqtt()
threading.Thread(target=_refresh_discovery, daemon=True).start()


@app.route("/api/discovered")
def api_discovered():
    with _disc_lock:
        nodes        = [dict(n) for n in _disc_cache["rnstatus"]]
        halow        = [dict(n) for n in _disc_cache["halow"]]
        rnstatus_ts  = _disc_cache["rnstatus_ts"]
    rnstatus_ok = rnstatus_ts > 0 and (time.time() - rnstatus_ts) < DISCOVERY_STALE_SECS
    for p in PINNED_NODES:
        if p.get("lat") is None or p.get("lon") is None:
            continue
        node = {
            "name":   p.get("name", "Pinned Node"),
            "type":   p.get("type", "RNode"),
            "lat":    p["lat"],
            "lon":    p["lon"],
            "pinned": True,
        }
        for k in ("frequency", "bandwidth", "sf", "cr", "height", "remote", "port", "hash", "b32", "lxmf"):
            if k in p:
                node[k] = p[k]
        nodes.append(node)
    gateway = _gateway_position()
    if gateway:
        nodes.append({
            "name": "NOEMA Gateway",
            "type": "RNode",
            "lat": gateway["lat"],
            "lon": gateway["lon"],
            "pinned": True,
            "gateway": True,
        })
    nodes.extend(halow)
    _enrich_geo(nodes)
    resp = jsonify(nodes)
    resp.headers["X-Rnstatus-OK"] = "1" if rnstatus_ok else "0"
    # This is the largest response the app serves (~170 KB) and every client re-polls it
    # on a 5 minute timer, so make the poll conditional: unchanged node lists come back
    # as a bodiless 304.
    #
    # The flag is folded into the ETag deliberately. A wedged rnsd leaves the node list
    # byte identical (it is served from cache) while rnstatus_ok flips to 0; hashing the
    # body alone would return 304, and the browser would replay the *cached* headers,
    # so X-Rnstatus-OK would stay "1" and the warning banner would never appear.
    flag = b"1" if rnstatus_ok else b"0"
    resp.set_etag(hashlib.sha1(resp.get_data() + flag).hexdigest())
    # "no-cache" means revalidate every time, not "do not store" -- which is what turns
    # each poll into a conditional request rather than a blind refetch.
    resp.headers["Cache-Control"] = "no-cache"
    return resp.make_conditional(request)


FAVICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "favicon.ico")


@app.route("/favicon.ico")
def favicon():
    return send_file(FAVICON_PATH, mimetype="image/vnd.microsoft.icon")


@app.route("/api/geojson")
def api_geojson():
    since     = request.args.get("since",     0,    type=int)
    from_ts   = request.args.get("from_ts",   None, type=int)
    to_ts     = request.args.get("to_ts",     None, type=int)
    direction = request.args.get("direction", "up")   # "up" or "down"
    dl = (direction == "down")
    if not _db_present():
        return jsonify({"type": "FeatureCollection", "features": []})
    try:
        con = get_db()
        rssi_col  = "dl_rssi AS rssi, dl_snr AS snr" if dl else "rssi, snr"
        # 'relay' points carry downlink stats too (a site bot's own probe reply, forwarded
        # to the aggregator) — that's the normal case, since a phone talks to its local site
        # bot, not the aggregator directly. Restricting to source='probe' here excluded
        # basically all real-world downlink data.
        dl_filter = " AND source IN ('probe', 'relay') AND dl_rssi IS NOT NULL" if dl else ""
        base      = f"SELECT lat, lon, {rssi_col}, q, ts, source FROM points"
        if from_ts is not None:
            rows = con.execute(
                f"{base} WHERE ts >= ? AND ts <= ?{dl_filter} ORDER BY ts",
                (from_ts, to_ts if to_ts is not None else int(time.time())),
            ).fetchall()
        else:
            rows = con.execute(
                f"{base} WHERE ts > ?{dl_filter} ORDER BY ts",
                (since,),
            ).fetchall()
        con.close()
        features = [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [r["lon"] / 1_000_000, r["lat"] / 1_000_000],
                },
                "properties": {
                    "rssi":   r["rssi"],
                    "snr":    r["snr"],
                    "q":      r["q"],
                    "ts":     r["ts"],
                    "source": r["source"],
                },
            }
            for r in rows
        ]
        return jsonify({"type": "FeatureCollection", "features": features})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/geo")
def api_geo():
    with _geo_lock:
        return jsonify(dict(_geo_cache))


def _send_asset(subdir, filename):
    """Serve a basemap asset, refusing any path that escapes its directory."""
    root = os.path.realpath(os.path.join(STATIC_DIR, subdir))
    path = os.path.realpath(os.path.join(root, filename))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    # conditional=True gives Range support, which is what pmtiles.js relies on to pull
    # individual tiles out of the single archive file.
    return send_file(path, conditional=True)


BASEMAP_PATH = os.path.join(STATIC_DIR, "tiles", "basemap.pmtiles")


def _basemap_version():
    """Cache key for the basemap archive: its mtime and size.

    The archive is republished at a fixed path, and /tiles/ is served with a week of
    cache lifetime. Swapping it in place therefore left every browser that had already
    loaded the map holding byte ranges of the previous archive under the same URL, with
    no reason to revalidate -- so pmtiles read a root directory describing the old file
    and asked for offsets that meant something else entirely in the new one. Nothing
    rendered, and the server looked healthy throughout, because it was.

    Versioning the URL makes each archive a distinct cache entry, which is what lets the
    long max-age on /tiles/ be correct rather than dangerous.
    """
    try:
        st = os.stat(BASEMAP_PATH)
        return f"{int(st.st_mtime)}-{st.st_size}"
    except OSError:
        return "0"


@app.route("/basemap/<name>.json")
def basemap_style(name):
    """A GL style with the pmtiles source URL pinned to the current archive version."""
    root = os.path.realpath(os.path.join(STATIC_DIR, "styles"))
    path = os.path.realpath(os.path.join(root, name + ".json"))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    with open(path, encoding="utf-8") as f:
        style = json.load(f)
    version = _basemap_version()
    for src in (style.get("sources") or {}).values():
        url = src.get("url")
        if isinstance(url, str) and url.startswith("pmtiles://"):
            if PUBLIC_URL and url.startswith("pmtiles:///"):
                url = "pmtiles://" + PUBLIC_URL + url[len("pmtiles://"):]
            src["url"] = url + ("&" if "?" in url else "?") + "v=" + version
    if PUBLIC_URL:
        for key in ("glyphs", "sprite"):
            val = style.get(key)
            if isinstance(val, str) and val.startswith("/"):
                style[key] = PUBLIC_URL + val
    resp = jsonify(style)
    # The style itself must never be cached, or it would pin an outdated version token
    # and reintroduce exactly the problem it exists to solve.
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.route("/tiles/<path:filename>")
def tiles(filename):
    return _send_asset("tiles", filename)


@app.route("/fonts/<path:filename>")
def fonts(filename):
    return _send_asset("fonts", filename)


@app.route("/sprite/<path:filename>")
def sprite(filename):
    return _send_asset("sprite", filename)


@app.route("/vendor/<path:filename>")
def vendor(filename):
    root = os.path.realpath(VENDOR_DIR)
    path = os.path.realpath(os.path.join(root, filename))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    return send_file(path, conditional=True)


# ---- Community overlay -------------------------------------------------------
#
# Uploads never touch the bot database. Each one is parsed and vetted here
# (overlay_validate), stored with its contributor and batch (overlay_store), and shown
# on the public layer only if it passed. The write routes take a bearer token, never a
# cookie, so a page on another site has nothing to ride on; they send no CORS headers.

_OVERLAY_ERRORS = {   # StoreError code -> HTTP status
    "forbidden": 403, "bad_invite": 403, "bad_name": 400, "duplicate_file": 409,
    "quota_batches": 429, "quota_rows": 429, "invite_quota": 429,
    "overlay_full": 503, "no_such_batch": 404, "no_such_contributor": 404,
}


def _overlay_reply(payload, status=200):
    resp = make_response(jsonify(payload), status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _overlay_off():
    return jsonify({"error": "not found"}), 404


def _bearer():
    h = request.headers.get("Authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


def _overlay_user():
    return _overlay.authenticate(_bearer())


@app.errorhandler(413)
def _too_large(_e):
    return _overlay_reply({"error": "too_large", "max_bytes": OVERLAY_MAX_UPLOAD}, 413)


@app.route("/api/overlay/upload", methods=["POST"])
def overlay_upload():
    if not OVERLAY_ENABLED:
        return _overlay_off()
    who = _overlay_user()
    if not who:
        return _overlay_reply({"error": "unauthorized"}, 401)
    try:
        _overlay.check_batch_quota(who)          # before the body is read or parsed
        data = request.get_data(cache=False)
        if not data:
            return _overlay_reply({"error": "empty"}, 400)
        digest = hashlib.sha256(data).hexdigest()
        try:
            result = overlay_validate.validate_batch(
                overlay_validate.parse_export(request.args.get("name", "")[:100], data,
                                              _overlay_limits),
                int(time.time() * 1000), _overlay_limits)
        except overlay_validate.ParseError as e:
            # Recorded like any rejected batch, so poking the parser is not free either.
            result = overlay_validate.BatchResult([], Counter(), 0, 0, e.args[0])
        out = _overlay.add_upload(who, digest, result)
    except StoreError as e:
        return _overlay_reply({"error": e.args[0]}, _OVERLAY_ERRORS.get(e.args[0], 400))
    print(f"[OVERLAY] contributor={who['id']} batch={out['batch']} accepted={out['accepted']} "
          f"duplicates={out['duplicates']} rejected={out['rejected']} error={out['error']}",
          flush=True)
    return _overlay_reply(out, 422 if out["error"] else 200)


@app.route("/api/overlay/me")
def overlay_me():
    """The caller's own standing: lets the app verify a pasted token and show what is left today."""
    if not OVERLAY_ENABLED:
        return _overlay_off()
    who = _overlay_user()
    if not who:
        return _overlay_reply({"error": "unauthorized"}, 401)
    out = _overlay.quota_status(who)
    out["max_rows_per_file"] = _overlay_limits.max_rows
    return _overlay_reply(out)


@app.route("/api/overlay/invite", methods=["POST"])
def overlay_invite():
    if not OVERLAY_ENABLED:
        return _overlay_off()
    who = _overlay_user()
    if not who:
        return _overlay_reply({"error": "unauthorized"}, 401)
    try:
        code = _overlay.create_invite(who["id"])
    except StoreError as e:
        return _overlay_reply({"error": e.args[0]}, _OVERLAY_ERRORS.get(e.args[0], 400))
    print(f"[OVERLAY] contributor={who['id']} minted an invite", flush=True)
    return _overlay_reply({"code": code})


@app.route("/api/overlay/redeem", methods=["POST"])
def overlay_redeem():
    if not OVERLAY_ENABLED:
        return _overlay_off()
    body = request.get_json(silent=True)         # JSON content-type only: not a "simple" cross-site POST
    if not isinstance(body, dict) or not isinstance(body.get("code"), str) \
            or not isinstance(body.get("name"), str):
        return _overlay_reply({"error": "bad_request"}, 400)
    try:
        cid, token = _overlay.redeem_invite(body["code"][:100], body["name"])
    except StoreError as e:
        return _overlay_reply({"error": e.args[0]}, _OVERLAY_ERRORS.get(e.args[0], 400))
    print(f"[OVERLAY] invite redeemed: new contributor={cid}", flush=True)
    return _overlay_reply({"token": token})


@app.route("/api/overlay")
def overlay_samples():
    """The published community overlay as GeoJSON. Nothing in it says who sent a sample."""
    if not OVERLAY_ENABLED:
        return _overlay_off()
    bbox = None
    if request.args.get("bbox"):
        try:
            bbox = tuple(float(x) for x in request.args["bbox"].split(","))
        except ValueError:
            bbox = ()
        if not (len(bbox) == 4 and all(math.isfinite(x) for x in bbox)
                and -90 <= bbox[1] <= bbox[3] <= 90 and abs(bbox[0]) <= 180 and abs(bbox[2]) <= 180):
            return jsonify({"error": "bad bbox, expected west,south,east,north"}), 400
    since = request.args.get("since", None, type=int)
    limit = max(1, min(request.args.get("limit", 20000, type=int), OVERLAY_MAX_RESPONSE_ROWS))
    rows = _overlay.published(bbox, since, limit)
    fc = OverlayStore.public_geojson(rows)
    fc["truncated"] = len(rows) >= limit
    resp = make_response(jsonify(fc))
    resp.headers["Cache-Control"] = "public, max-age=15"   # a takedown is visible within this
    return resp


_CORS_PREFIXES = ("/basemap/", "/tiles/", "/fonts/", "/sprite/", "/static/")

@app.after_request
def _asset_cors(resp):
    """Let other installs' pages read the basemap assets when this one publishes them.

    In production nginx answers /tiles /fonts /sprite /static itself and carries the
    same headers (map/nginx/wardrive-map-assets.conf); only /basemap/ reaches here.
    Range is listed because pmtiles.js reads the archive in byte ranges, and the
    exposed headers are the ones it inspects on the way back.
    """
    if PUBLIC_URL and request.path.startswith(_CORS_PREFIXES):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Range"
        resp.headers["Access-Control-Expose-Headers"] = \
            "Content-Range, Content-Length, ETag, Accept-Ranges"
    return resp


def _serve():
    """Run the app under waitress, falling back to Werkzeug if it is unavailable.

    Waitress is a single process with a thread pool, which this app requires: the
    discovery refresher at the bottom of this file is a background thread, and
    _geo_cache/_disc_cache/_halow_cache are module-level state behind locks. Under a
    forking server every worker would run its own refresher -- multiplying the rnstatus
    load -- and clients would see whichever worker's cache they happened to reach.
    """
    try:
        from waitress import serve
    except ImportError:
        print("[WARN] waitress not installed; falling back to the Werkzeug dev server",
              flush=True)
        app.run(host=HOST, port=PORT, debug=False)
        return
    print(f"Map server starting on http://{HOST}:{PORT} (waitress, {SERVER_THREADS} threads)",
          flush=True)
    # clear_untrusted_proxy_headers defaults to True, which would strip the
    # X-Forwarded-* headers nginx sets before ProxyFix (line ~208) ever sees them,
    # breaking scheme and host detection. nginx is on loopback and is the only client,
    # so pass the headers through and let ProxyFix interpret them as it already does.
    # waitress buffers a whole request body before the app sees it and defaults to
    # allowing 1 GB, so cap it here too; MAX_CONTENT_LENGTH only applies once Flask runs.
    serve(app, host=HOST, port=PORT, threads=SERVER_THREADS,
          ident="wardrive-map", clear_untrusted_proxy_headers=False,
          max_request_body_size=OVERLAY_MAX_UPLOAD + 64 * 1024)


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print(f"[INFO] No bot database at {DB_PATH} — live data stays empty until wardrive_bot writes "
              "it; exported files from the app can still be opened in the page")
    _serve()
