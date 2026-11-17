## 1.1.19

### Added
- **Uninstall script.** `uninstall.sh` beside `install.sh` stops NOEMA services and removes units, the install directory, `lxmf-tools`, rBrowser, `/etc/noema`, and wardrive configs/DB. Reticulum and Nomadnet identities stay unless you pass `--purge`.
- **System → Support.** Builds a sanitized diagnostic zip (journals, versions, redacted configs — no identities or passwords) and downloads it in the browser. The report is not sent to Telegram or GitHub.

### Changed
- **Wardrive map port comes from `wardrive_map.json`.** `/api/wardrive/map` reads `port` and `listen_on`; the sample config and `map_server.py` default to **5001**. A manual port change still drives the embedded map. Reinstall does not overwrite an existing `wardrive_map.json`.
- **Clear wardrive DB from the map tab.** A **Clear DB** button above the embedded map deletes all rows in the coverage SQLite database (with confirm) and reloads the map.
- **Logs tab lists wardrive services.** The service selector includes `wardrive_bot` and `wardrive_map` (collector + map journals).
- **Wardrive map finds `rnstatus` after a clean `install.sh`.** systemd units did not put `.venv/bin` on `PATH`, so the map logged `No such file or directory: 'rnstatus'` and showed the red discovery banner even when beacons worked. `map_server.py` now resolves the venv binary; `install.sh` also sets `PATH` for services. The beacon UI hides that discovery banner.
- **NOEMA Beacon 0.1.5.** TX power is set in the app from **0 to 30 dBm**. APK: `noema-beacon/noema-beacon.apk`.
- Dashboard version reports `1.1.19`. Changed files: `dashboard/app.py`, `dashboard/index.html`, `wardrive/map/map_server.py`, `wardrive/config/wardrive_map.json`, `uninstall.sh`, `install.sh`, `noema-beacon/noema-beacon.apk`.

Docs for this release are in the wiki (Wardrive port, Support zip download, uninstall, Beacon power 0–30 dBm), not in README.
