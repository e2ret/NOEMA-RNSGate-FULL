# NOEMA RNSGate FULL

<img width="400" alt="NOEMA RNSGate FULL" src="https://github.com/user-attachments/assets/81fec1ae-41e7-4f3e-987a-691f9111e0ea" />


**Full-featured Reticulum/LXMF gateway with Radio Observatory dashboard**

[![Translate](https://img.shields.io/badge/Translate-To_Your_Language-red)](https://translate.google.com/translate?sl=auto&u=https://github.com/e2ret/NOEMA-RNSGate-FULL)

FULL is a sibling to [NOEMA RNSGate Lite](https://github.com/e2ret/NOEMA-RNSGate-Lite) — the key architectural difference is that the dashboard itself owns the Reticulum instance directly, giving it real access to RF telemetry from RNode interfaces.

### Key difference from Lite

| | Lite | FULL |
|---|---|---|
| Reticulum owner | `rnsd` (separate process) | `dashboard` (Flask app) |
| RSSI/SNR access | ❌ always null | ✅ real values |
| RF telemetry history | ❌ | ✅ |
| Node Tracker | ❌ | ✅ |
| RF Activity Log | ❌ | ✅ |
| Interference Detection | ❌ | ✅ |
| Network Map (dartboard) | ❌ | ✅ |
| PTY Terminal | ❌ | ✅ |

### Features

- **Radio Observatory** — real-time RSSI/SNR/Noise/Airtime graphs from RNodeInterface
- **RF Activity Log** — every received LoRa packet with signal bar, RSSI, SNR; Noise floor changes
- **Interference Events** — automatic detection of noise floor spikes (>3 dBm above baseline)
- **Node Tracker** — all Reticulum nodes heard via announce, dartboard network map with hop rings
- **PTY Terminal** — full bash terminal in the browser (Logs panel)
- **LXMF Chat** — send/receive messages via MQTT bridge
- **Nomadnet** — page browser and node hosting
- **I2P integration** — anonymous incoming tunnel via i2pd
- **System panel** — services control, Reticulum config editor, backup/restore

### Quick Install

```bash
git clone https://github.com/e2ret/NOEMA-RNSGate-FULL.git
cd NOEMA-RNSGate-FULL
sudo bash install.sh
```

After installation, open `http://<your-ip>:8081`

### Services

| Service | Description |
|---|---|
| `dashboard` | Main Flask app — owns Reticulum instance |
| `noema_lxmf_bridge` | LXMF ↔ MQTT bridge |
| `nomadnet` | Nomadnet node daemon |
| `rbrowser` | rBrowser web UI (port 5000) |
| `i2pd` | I2P daemon |

### Radio Observatory — API endpoints

| Endpoint | Description |
|---|---|
| `GET /api/rnode/history` | RSSI/SNR/Noise/Airtime ring buffer (20 min) |
| `GET /api/rnode/rf_stats` | Current RF metrics |
| `GET /api/rnode/activity` | RF packet + Noise activity log |
| `GET /api/rnode/interference` | Noise spike events |
| `GET /api/nodes` | Node tracker — all heard announces |
| `GET /api/nodes/debug` | Announce handler state |

### License

MIT

---

## Support the project
If you find this useful, donations are welcome.

![USDT TRC20](https://img.shields.io/badge/USDT_(TRC20)-26A17B?style=flat&logo=tether&logoColor=white) `TD89XXL9ehwhp4WysfqHSBGJjxxdoaVsYD`

![TON](https://img.shields.io/badge/TON-0098EA?style=flat&logo=ton&logoColor=white) `UQCmROnKeaWIt5Uxu3MTebKUjYQHuvVeyOauWdVn6srUWKX8`

[![Boosty](https://img.shields.io/badge/Boosty-E55F2A?style=flat&logo=boosty&logoColor=white)](https://boosty.to/noemarns/donate)

## Contacts

[![Telegram](https://img.shields.io/badge/Telegram-2CA5E0?style=flat&logo=telegram&logoColor=2CA5E0&labelColor=white&label=)](t.me/https://t.me/reticulum_belgorod/70) 

![LXMF](https://img.shields.io/badge/LXMF-222222?style=flat) `3c4d222ee3acca1b386f5c2ad7ff1c6f`

[![GitHub](https://img.shields.io/badge/GitHub-121011?style=flat&logo=github&logoColor=121011&labelColor=white&label=)](https://github.com/e2ret)

