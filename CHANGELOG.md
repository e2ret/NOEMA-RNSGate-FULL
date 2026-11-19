## 1.1.20

> [!CAUTION]
> **Reticulum 1.6.x stamps** — see `README.md` in this pack. After `pip install -U rns` to **1.6.1**, the first gateway start may peg CPU for several minutes (one-time stamp generation). **1.6.2** will enforce stamps and drop unstamped traffic.

### Added
- **“What’s in this update?”** next to the NOEMA update badge / Update button — opens the GitHub compare (or releases) for the pending commits.

### Fixed
- **Chat compose bar stays visible with many contacts.** A long contact list no longer stretches the chat grid and clips the message input. Contacts scroll inside their column; the compose bar stays pinned at the bottom.

### Changed
- **Wardrive collector branding.** Welcome / display name no longer mentions Sideband. Default text refers to **NOEMA Beacon**. Existing installs that still have “Sideband” in `/etc/reticulum/wardrive_bot.json` are refreshed on reinstall.
- **Dashboard copy** no longer names third-party LXMF apps in chat/bridge address hints (generic “LXMF client” wording).
- **Wardrive map layer picker (OSM provider).** With `basemap_provider: "osm"`, the map shows a tileset dropdown: **Dark** / **Liberty** / **Light** via [OpenFreeMap](https://openfreemap.org/) (no API key), plus **OSM** raster with building footprints. Choice is kept in the browser. Changing the layer only swaps the map tiles — page chrome (panels, stream) stays on its own theme.
- **Signal Overlay** is a simple on/off for gateway-heard beacon heat. Uplink / Downlink / Both cycling removed (probe-mode leftover).
