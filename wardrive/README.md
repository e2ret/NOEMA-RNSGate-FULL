# rns-wardrive-tools

A collection of tools for wardriving LoRa coverage using the [Reticulum Network Stack](https://reticulum.network) and [Sideband](https://github.com/markqvist/Sideband).

To feed location data to the collector bot you can use either the Sideband plugin below or the dedicated Android app, [**rns-wardrive-app**](https://git.rns.moscow/deuxyeux/rns-wardrive-app), which provides a live map, transmission log, and a bi-directional Probe mode for two-way RF link quality surveys.

## Components

### `plugin/` — Sideband Plugin

A Sideband service plugin that runs on your mobile device and periodically sends your GPS location to the collector bot over RNS. If you'd rather use a standalone app instead of running it inside Sideband, see [rns-wardrive-app](https://git.rns.moscow/deuxyeux/rns-wardrive-app).

**Installation:**

Copy `wardrive_plugin.py` and `wardrive_config.json` to your Sideband plugins directory (configured in Sideband settings under *Plugins path*).

**Configuration (`wardrive_config.json`):**

```json
{
  "announce_interval_secs": 30,
  "announce_mode": "auto",
  "send_mode": "packet",
  "bot_hashes": ["<bot collector destination hash>"]
}
```

| Option | Values | Description |
|---|---|---|
| `announce_interval_secs` | integer (min 15) | How often to send location in auto mode |
| `announce_mode` | `auto` / `manual` | `auto`: sends on interval; `manual`: sends when you press Sideband's announce button |
| `send_mode` | `announce` / `packet` | `announce`: broadcasts on the network; `packet`: sends directly and privately to the bot |
| `bot_hashes` | list of hex strings | Collector destination hashes of bots to send to (packet mode only) |

In **packet mode**, location data is sent encrypted directly to each listed bot — it is not broadcast to the wider network. RSSI/SNR are still recorded on the receiving end.

In **announce mode**, location is broadcast as a `wardrive.beacon` announce visible to all nodes.

---

### `bot/` — Collector Bot

A server-side RNS daemon that listens for incoming location data, records RF signal statistics, and responds to LXMF commands.

**Requirements:** `rns`, `lxmf`, `rnsd` running

**Installation:**

Copy `wardrive_bot.py` to `/opt/reticulum/bin/` (or any location on your `$PATH`).

**Usage:**

```bash
python3 wardrive_bot.py
```

The bot writes to `~/wardrive_bot.db` (SQLite) and logs to `/etc/reticulum/wardrive_bot_logfile`.

**LXMF commands** (send via Sideband or any LXMF client):

| Command | Response |
|---|---|
| `ping` | `Pong! [14:32:01, RSSI: -74dBm, SNR: 6.5dB]` — RF stats included if message arrived over LoRa |
| `status` | Point count and timestamp of last received beacon |
| `nodes` | Count of discoverable nodes within the configured radius |
| `destinations` | Collector/probe destination hashes, plus the relay (aggregator) hash if this bot is the aggregator |
| `announce` | Reannounces all of the bot's destinations after a short randomized delay |

---

### `map/` — Coverage Map Server

A Flask web server that reads the bot's database and serves a live interactive coverage heatmap.

**Viewing app exports (no bot needed):** the [rns-wardrive-app](https://git.rns.moscow/deuxyeux/rns-wardrive-app) can export its local samples as GeoJSON, CSV or GPX without any bot involved. Click **📂 Open file** in the map (or drop one or more files on the page) to show them as uplink/downlink heatmaps; probes nobody answered appear as red dots, the coverage holes. The files are parsed in the browser and nothing is uploaded; **✕ Close file** returns to the live data. The bot database is optional: if `db_path` does not exist the map starts with empty live data and works purely as a file viewer.

**Requirements:** `flask`, `rns`, and `paho-mqtt` if `halow_mqtt` is configured (HaLow nodes, below). The basemap can be OpenStreetMap, borrowed from another install, or a self-hosted tileset (see *Basemap providers* below)

**Installation:**

Copy `map_server.py`, `favicon.ico` and the `vendor/` directory to `/opt/reticulum/bin/`, and `wardrive_map.json` to `/etc/reticulum/`. `vendor/` holds MapLibre GL JS and pmtiles.js and must sit beside `map_server.py`. To use the community overlay (below) also copy `overlay_validate.py`, `overlay_store.py`, `overlay_admin.py` and `overlay_admin_web.py` beside it.

**Configuration (`wardrive_map.json`):**

```json
{
  "db_path":       "~/wardrive_bot.db",
  "discovery_dir": "/etc/reticulum/storage/discovery/interfaces",
  "listen_on":     "0.0.0.0",
  "port":          5000,
  "page_title":    "Wardrive Coverage Map",
  "basemap_provider": "osm",
  "initial_lat":   null,
  "initial_lon":   null,
  "initial_zoom":  null
}
```

Set `initial_lat`, `initial_lon`, and `initial_zoom` to pre-position the map on load; leave all three as `null` to auto-fit to recorded points.

**Usage:**

```bash
python3 map_server.py
```

Serves on `http://0.0.0.0:5000` by default.

**Features:**
- Live heatmap of recorded coverage points, coloured by RSSI
- Live data stream panel showing incoming packets in real time (source, coordinates, RSSI/SNR/Q)
- Date range filter with day/week/month/year presets
- Light/dark UI theme with tileset selector (Green, Voyager, Light, OSM, Dark) on vector basemaps
- HaLow RNodes — the nodes publish their telemetry (name, position, frequency, firmware, packet counters) as JSON to an MQTT broker (topic `rnode-halow/telemetry`), and the map subscribes to it; set `halow_mqtt` in the config (see `map/wardrive_map.json`; needs `paho-mqtt` and `halow_telemetry.py`). The nodes' login is public, so everything received is validated, bounded and reduced to a character allow-list before it is shown. `python3 map/test_halow.py` runs the parser tests
- RNS infrastructure discovery panel — shows RNodes, Backbone, and I2P interfaces that have GPS coordinates, sourced from the local RNS discovery cache
- Toggleable node labels per interface type
- Operator LXMF address (`discovery_lxmf_address`) shown in the popup and node list of every node that announces one; the node search matches it too
- Announced implementation and version (e.g. `RNS 1.5.6`) shown in the popup

**Community overlay (optional)**

Contributors can publish exported reports to a shared layer that everyone sees on the map together with the **Signal Overlay** (the same Uplink / Downlink choice applies to both). It is off unless `"overlay_enabled": true`. Opening a file in the page still uploads nothing; sharing is a separate, explicit **Share** step.

A report file is unsigned and can be edited by whoever holds it, so no check can prove a measurement is real. The design instead limits who can send, rejects everything that cannot be real, caps volume, and makes removal total:

- **Invite-only.** Access is a per-person bearer token (stored only as a SHA-256). You hand out the first invites with `overlay_admin.py invite`; contributors can invite a few others (`max_invites`), and the invite chain is recorded so one ban can cascade.
- **Server-side vetting.** The server re-parses the raw file itself (the browser's parse is never trusted) and rejects out-of-range RSSI/SNR (the wire format's limits), future or ancient timestamps, tracks that jump faster than `max_speed_ms`, poor GPS fixes, malformed ids, a `replied` flag that disagrees with the signal fields, and constant-signal files. A file with more than `max_reject_frac` invalid rows is refused whole. Overlapping exports are fine: duplicates are dropped without counting against the sender.
- **Quotas.** Per contributor per rolling day (`max_rows_day`, `max_batches_day`; rejected uploads count too) and a whole-overlay ceiling (`max_total_rows`). nginx adds per-client rate limits and the body cap (`map/nginx/wardrive-map-upload.conf`).
- **Programmatic clients.** The map page is one client; the Android app is another. The API is `POST /api/overlay/redeem` (invite code and a name, returns a token), `POST /api/overlay/upload?name=FILE` (raw GeoJSON/CSV/GPX body, `Authorization: Bearer TOKEN`) and `GET /api/overlay/me` (the token's name and what is left today: rows, batches, invites; 401 if the token is unknown or revoked).
- **Separate storage.** Everything lives in its own SQLite file (`overlay_db_path`), never the bot's. Each sample belongs to a batch and each batch to a contributor.
- **Takedown.** `overlay_admin.py remove-batch ID` (or the web admin's *Remove session*) removes one shared session; `ban ID [--cascade]` locks a contributor out and deletes everything they sent (and, with `--cascade`, everyone they invited). The public endpoint caches for 15 s.
- **Privacy of what is published.** Positions are rounded to ~1 m and times to the hour; contributor, session and responder ids are never exposed. Unanswered probes are stored but not drawn, so the layer does not trace routes.

```bash
overlay_admin.py invite                        # one-use code for a new contributor
overlay_admin.py contributors                  # who exists, who invited whom, how much they sent
overlay_admin.py batches [--contributor ID]    # recent sessions and why any were rejected
overlay_admin.py remove-batch ID
overlay_admin.py ban ID [--cascade]
overlay_admin.py limits ID --rows-day 50000
```

**Web admin.** `overlay_admin_web.py` shows contributors (who invited whom, usage against quota), invites, every shared session with the reasons it was rejected, and a plot of where a session's entries are, and it can act on them: mint an invite, add a contributor (the token is shown once), rename, change limits, rotate a token, ban (optionally with everyone they invited), reinstate, remove a session, void an invite. Every change goes to the service log and to an Activity table on the overview. Pages are built from a read-only database connection; changes are POSTs that go through the same `OverlayStore` code as the server and `overlay_admin.py`. Each POST needs a CSRF token tied to the signed-in user and must come from the page's own origin; ban, token rotation and session removal only run from a confirmation page that says what will be deleted. There is no JavaScript on the page. Start it with `--read-only` (or `"read_only": true` in the `overlay_admin` block) to get a view that refuses every change. It is a separate process that always binds `127.0.0.1` (there is no option to widen that) and refuses requests addressed to any host name it was not configured with (DNS rebinding). `overlay_admin.py` stays available from a shell and works when the web page is down.

Two ways to use it:

- *Through an SSH tunnel* (nothing public, SSH is the only credential):
  ```bash
  ssh -L 8850:127.0.0.1:8850 srv1 /opt/reticulum/bin/overlay_admin_web.py
  # then open http://127.0.0.1:8850/   (Ctrl+C in the ssh session stops it)
  ```
- *Published at `https://map.rns.moscow/overlay-admin/`, guarded by Authelia.* Run it as the `wardrive-overlay-admin` service (`init_scripts/`), set the `overlay_admin` block in `wardrive_map.json` (`public_hosts`, `base_path`), and include `map/nginx/wardrive-map-admin.conf` in the vhost. nginx forwards Authelia's user name in `X-Remote-User`, and the app returns 403 to any request without it, so a vhost that loses its auth lines fails closed. Authelia itself needs a session cookie for the `rns.moscow` domain and an access rule for `/overlay-admin`; the exact YAML is in the nginx snippet. The page shows exact coordinates and contributor names and can delete data, so restrict the rule to admins (and consider `two_factor` now that it can change things).

Not yet done: plausibility scoring against known node positions (path loss vs. distance) and cross-checking against neighbouring reports. Until then a careful forger who stays inside the physical limits is stopped by the invite chain, the quotas and takedown, not by the checks alone. Tests: `python3 map/test_overlay.py`.

**Basemap providers**

`basemap_provider` in `wardrive_map.json` chooses where the map background comes from:

| Provider | What it uses | Needs |
|---|---|---|
| `osm` | OpenStreetMap raster tiles from `tile.openstreetmap.org` | nothing |
| `raster` | Any XYZ raster tile server | `raster_tiles` |
| `remote` | The vector basemap of another install, fetched by the browser | `basemap_url` |
| `local` (default when the key is absent) | A tileset on this machine | `tileset_dir`, see *Self-hosted basemap* |

```json
"basemap_provider": "remote",
"basemap_url":      "https://map.rns.moscow"
```

```json
"basemap_provider": "raster",
"raster_tiles": {
  "label":       "My tiles",
  "url":         "https://tiles.example.org/{z}/{x}/{y}.png",
  "attribution": "&copy; Example",
  "max_zoom":    19,
  "tile_size":   256
}
```

`remote` gives the full feature set — five styles, the label toggle and label language —
with no tileset and no proxy: the browser loads styles, tiles, glyphs and sprites directly
from the other install. The raster providers show a single basemap, so the style selector
and label toggle are hidden and the theme button only recolours the page.

`tile.openstreetmap.org` is run on donated resources and its
[usage policy](https://operations.osmfoundation.org/policies/tiles/) allows light use
only. For a busy public map use `remote`, `raster` with a provider you have an agreement
with, or `local`.

With `osm`, `raster` and `remote` no `tileset_dir` is needed. Place names for discovered
nodes come from a separate dataset; build it with `map/geo/build_geodb.py` and point
`geo_db_path` at it, or nodes are simply listed without a location.

*Lending a basemap to other installs.* An install with a local tileset becomes usable as
someone else's `remote` provider once it sets

```json
"public_url": "https://map.example.org"
```

to the address browsers reach it at. Styles are then served with absolute URLs and
`/basemap/` answers cross-origin requests. If nginx serves the assets, it must send the
same CORS headers: `map/nginx/wardrive-map-cors.conf` is included from each location in
`wardrive-map-assets.conf` for that. Without `public_url` nothing changes.

**Self-hosted basemap**

The map renders with [MapLibre GL JS](https://maplibre.org) against vector tiles served
from this machine, using the [dark-matter](https://github.com/openmaptiles/dark-matter-gl-style)
OpenMapTiles style, alongside [positron](https://github.com/openmaptiles/positron-gl-style),
[osm-bright](https://github.com/openmaptiles/osm-bright-gl-style) and
[maptiler-basic](https://github.com/openmaptiles/maptiler-basic-gl-style). Nothing on the
page is fetched from a third party: tiles, glyphs, sprites, JavaScript, and reverse
geocoding are all local.

All five basemaps share the single vector tileset — a GL style is only a JSON recipe, so
each extra basemap costs a style file and a sprite sheet, not another copy of the map
data. The "no labels" entry is generated by dropping the symbol layers from Voyager.

Assets live outside the repo because the tileset is far too large to version:

| Path | Contents |
|---|---|
| `<tileset_dir>/static/tiles/basemap.pmtiles` | Vector basemap (planet z0-12 + Central FD z13-14) |
| `<tileset_dir>/static/fonts/` | Composed glyph stacks |
| `<tileset_dir>/static/sprite/` | Style sprite sheet |
| `<tileset_dir>/static/styles/` | Localized GL style JSON |
| `<tileset_dir>/static/js/` | maplibre-gl + pmtiles |
| `<tileset_dir>/static/js/` (cont.) | RTL text plugin for Arabic/Hebrew shaping |
| `<tileset_dir>/geo/cities.tsv.gz` | Offline reverse-geocoding dataset |
| `<tileset_dir>/build/` | Build intermediates — deliberately outside the served tree |

`tileset_dir` defaults to `/mnt/sdb1/tilesets` and is set in `wardrive_map.json`.
Only `static/` and `geo/` are read at runtime; keep font archives and unlocalized styles
in `build/` so they are not exposed over HTTP.

Every style renders `{name:nonlatin}` beside `{name:latin}`, so a worldwide basemap shows
place names in local scripts. The glyph stacks cover Cyrillic, Arabic, Hebrew, CJK, Hangul
and Devanagari; Arabic and Hebrew additionally need the RTL text plugin, which is loaded
lazily from local storage.

Build tooling lives in `map/tiles/` and `map/geo/`:

```bash
map/tiles/build.sh planet      # planet z0-12   (long; needs the planet .pbf)
map/tiles/build.sh cfd         # regional z13-14
map/tiles/build.sh merge       # stitch + convert to .pmtiles

map/tiles/compose_glyphs.py fonts-src fonts        # merge fontstacks for static serving
map/tiles/localize_style.py  style.json out.json   # strip third-party URLs from a GL style
map/tiles/make_nolabels.py   in.json out.json      # derive a label-free style variant
map/tiles/check_styles.py                          # verify styles against tiles/fonts/sprites
map/geo/build_geodb.py       geo/cities.tsv.gz     # refresh the geocoding dataset
```

`check_styles.py` is worth running after adding a basemap: a style that references a
source-layer the tileset lacks, or a fontstack with no glyph directory, renders as a map
quietly missing those features rather than raising an error.

Planetiler writes its scratch to `/var/tmp/planetiler-tmp` by default. Keep that on
fast storage — if the tileset volume is a spinning disk, building there is many times
slower than building on an SSD and copying the result across.

In production nginx serves `/tiles/`, `/fonts/`, `/sprite/`, and `/static/` directly
off disk (see `map/nginx/wardrive-map-assets.conf`); the Flask routes for those paths
are a fallback so the map still works when hitting the port directly. Serving the
`.pmtiles` archive requires HTTP range request support, which nginx provides for static
files natively — there is no tile server process.

---

### `init_scripts/` — Service Files

Systemd and OpenRC service files for both the bot and the map server.

**OpenRC:**
```bash
cp init_scripts/openrc/wardrive-bot  /etc/init.d/
cp init_scripts/openrc/wardrive-map  /etc/init.d/
chmod +x /etc/init.d/wardrive-bot /etc/init.d/wardrive-map
rc-update add wardrive-bot default
rc-update add wardrive-map default
```

**Systemd:**
```bash
cp init_scripts/systemd/wardrive-bot.service /etc/systemd/system/
cp init_scripts/systemd/wardrive-map.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now wardrive-bot wardrive-map
```

---

## Data Flow

```
[Sideband + plugin]
       |
       | wardrive.beacon announce  (announce mode, visible to all)
       | wardrive.collector packet (packet mode, encrypted to bot only)
       |
[wardrive_bot.py] ──── wardrive_bot.db ────[map_server.py]──── browser
       |
  LXMF replies (ping, status)
```
