# Wardrive in NOEMA RNSGate FULL

The wardrive collector receives the phone's GPS beacon over Reticulum/LoRa. It records the point and receive-side RSSI/SNR in `/etc/reticulum/wardrive_bot.db`. The dashboard remains the owner of the RNS instance; `wardrive_bot.service` attaches through the shared-instance interface.

## Phone setup

Use Sideband for Android and install the wardrive service plugin from `plugin/wardrive_plugin.py`. Copy `plugin/wardrive_config.json` into Sideband's configured plugins directory. Set `bot_hashes` to the Collector Destination shown by:

```sh
journalctl -u wardrive_bot -b | grep 'Collector Destination'
```

Set `announce_mode` to `auto` and `send_mode` to `packet` for private direct beacons to this gateway. GPS must be enabled and Sideband must be allowed to access location in the background. The plugin sends coordinates; it does not send the radio measurements. The gateway measures RSSI/SNR when each LoRa packet arrives.

## Map and data

Open **Wardrive map** in NOEMA's side menu. The map server listens on port `5001`; rBrowser remains on `5000`. The included config uses OpenStreetMap raster tiles, which require internet access. Collected samples remain local in the bot's SQLite database. Community uploads are disabled.

The collector and map logs are available with `journalctl -u wardrive_bot` and `journalctl -u wardrive_map`. The collector destination hash is unique to each gateway installation.

## Android app option

The separate [rns-wardrive-app](https://git.rns.moscow/deuxyeux/rns-wardrive-app) can export GeoJSON/CSV/GPX for viewing in the map and provides a bidirectional Probe mode. This release integrates the Sideband plugin collector and server map; it does not bundle the Android app.
