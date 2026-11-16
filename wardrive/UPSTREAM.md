# Wardrive upstream attribution

Integrated from [deuxyeux/rns-wardrive-tools](https://git.rns.moscow/deuxyeux/rns-wardrive-tools), commit `cbd40ad66dbc17770f8777a1783ae5be9f34aeb0`.

The upstream author granted permission to include these sources in NOEMA RNSGate FULL v1.1.17. The original project did not include a license file at the referenced commit. NOEMA-specific changes are marked in `bot/wardrive_bot.py`; NOEMA configuration lives in `config/`.

The Sideband plugin is in `plugin/`. Configure its `bot_hashes` with the Collector Destination printed by `journalctl -u wardrive_bot` after installation. The gateway map is available on port 5001; it does not use rBrowser's port 5000.
