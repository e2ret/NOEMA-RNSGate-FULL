## 1.1.27

### Fixed
- **Dashboard CPU stampede.** Concurrent `/api/rnstatus` + collector used to spawn many `rnstatus` processes at once (UI 500 / high CPU). Shared 8s cache + lock — one run at a time.

## 1.1.26

### Fixed
- **SDR frequency inputs.** Packet-mode MHz fields widened so values like `868.825` are not clipped.
- **SDR receiver badge.** Shows whether an RTL-SDR USB dongle is plugged in (connected / in use / missing), separate from “tools installed”.
- **SDR receiver badge false “not connected”.** If waterfall/packet is already running (or USB shows Realtek 0bda via sysfs), badge shows connected/in use — no longer depends only on flaky `lsusb` string match.
- **SDR waterfall look.** Colormap and spectrum line use Noema cyan (navy → ice) instead of green/amber heat.
- **SDR world region presets.** Waterfall + Packet Detect catalogs cover RU cities and world regions (EU/UK, US/CA, AU/NZ, UA, KZ, JP/KR/TW/SG, IN, CN, BR, 433) for Meshtastic, MeshCore and Reticulum/RNode — with reference markers on the plot.

## 1.1.25

### Fixed
- **OSM info “hole”.** Collapsed attribution is a proper 32×32 “i” button (no empty dark pill, readable icon on light maps).

## 1.1.24

### Fixed
- **Discovery tab.** Icon-only control (no vertical “Discovery” label); status dots kept.

## 1.1.23

### Fixed
- **Wardrive map bottom controls.** Theme / size / tileset sit in a clear bar and no longer merge with OSM credit.
- **OSM attribution.** Shows as an info icon only; full OpenStreetMap text opens on tap.

## 1.1.22

### Fixed
- **English UI still showed Russian.** Sidebar clock and chat timestamps followed a hardcoded `ru` locale; wardrive map tileset names (Dark / Light / OSM) could stay Russian after switching the dashboard to EN because English label defaults were poisoned.

## 1.1.21

### Added
- **Home panel toggles on the Services page.** Buttons at the top show/hide Globe, Services, Architecture; remaining columns stretch. Kept in the browser.
- **Globe animation pause.** Play/pause on the globe stops auto-rotate and pulses; drag still works.

### Fixed
- **Mobile Wardrive map.** Beacon toolbar no longer overlaps; map is full-width; beacons are a collapsible bottom bar (tap header to expand); Signal Overlay top-right.
- **Mobile Chat.** Full-height layout so contacts / messages / compose stay usable.
- **Mobile Nomadnet editor.** The page/script textarea stretches to fill the remaining screen height instead of staying a short strip.
- **Mobile Configs tabs.** LXMF / Nomadnet / Reticulum tabs wrap like System (no horizontal clip).
- **Main menu height.** Sidebar nav items match System control height (32px), not the shorter compact padding.
- **Home panel toggles.** On phone, Globe / Services / Architecture always stack (no cramped 2-column); without Globe the side panel fills the row; toggle buttons match control height.

## 1.1.20

> [!CAUTION]
> **Reticulum 1.6.x stamps** — see `README.md` in this pack. After `pip install -U rns` to **1.6.1**, the first gateway start may peg CPU for several minutes (one-time stamp generation). **1.6.2** will enforce stamps and drop unstamped traffic.

### Added
- **“What’s in this update?”** next to the NOEMA update badge / Update button — opens the GitHub compare (or releases) for the pending commits.

### Fixed
- **Chat compose bar stays visible with many contacts.** A long contact list no longer stretches the chat grid and clips the message input. Contacts scroll inside their column; the compose bar stays pinned at the bottom.

### Changed
- **Wardrive bot is beacon-only (0.2.0).** Listens for `wardrive.beacon` from NOEMA Beacon and writes the map DB. Removed LXMF chat, collector, probe, ping, and aggregator destinations (no more multi-destination announce spam). Config is just `display_name` + `db_path`.
- **Wardrive collector branding.** Display name no longer mentions Sideband. Existing installs that still have “Sideband” in `/etc/reticulum/wardrive_bot.json` are refreshed on reinstall.
- **Dashboard copy** no longer names third-party LXMF apps in chat/bridge address hints (generic “LXMF client” wording).
- **Wardrive map layer picker (OSM provider).** With `basemap_provider: "osm"`, the map shows a tileset dropdown: **Dark** / **Liberty** / **Light** via [OpenFreeMap](https://openfreemap.org/) (no API key), plus **OSM** raster with building footprints. Choice is kept in the browser. Changing the layer only swaps the map tiles — page chrome (panels, stream) stays on its own theme.
- **Signal Overlay** is a simple on/off for gateway-heard beacon heat. Uplink / Downlink / Both cycling removed (probe-mode leftover).
