# NOEMA RNSGate FULL

**Full-featured Reticulum/LXMF gateway with Radio Observatory dashboard**

FULL is a sibling to [NOEMA RNSGate Lite](https://github.com/e2ret/NOEMA-RNSGate-Lite) — the key architectural difference is that the dashboard itself owns the Reticulum instance directly, giving it real access to RF telemetry from RNode interfaces.

---

## Key difference from Lite

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

---

## Features

- **Radio Observatory** — real-time RSSI/SNR/Noise/Airtime graphs from RNodeInterface
- **RF Activity Log** — every received LoRa packet with signal bar, RSSI, SNR; Noise floor changes
- **Interference Events** — automatic detection of noise floor spikes (>3 dBm above baseline)
- **Node Tracker** — all Reticulum nodes heard via announce, dartboard network map with hop rings
- **PTY Terminal** — full bash terminal in the browser (Logs panel)
- **LXMF Chat** — send/receive messages via MQTT bridge
- **Nomadnet** — page browser and node hosting
- **I2P integration** — anonymous incoming tunnel via i2pd
- **System panel** — services control, Reticulum config editor, backup/restore

---

## Hardware

- Any Linux server/SBC (tested on x86-64 and ARM)
- RNode (LoRa) connected via TCP or USB — optional but required for RF telemetry

---

## Quick Install

```bash
git clone https://github.com/e2ret/NOEMA-RNSGate-FULL.git
cd NOEMA-RNSGate-FULL
sudo bash install.sh
```

After installation, open `http://<your-ip>:8081`

---

## Services

| Service | Description |
|---|---|
| `dashboard` | Main Flask app — owns Reticulum instance |
| `noema_lxmf_bridge` | LXMF ↔ MQTT bridge |
| `nomadnet` | Nomadnet node daemon |
| `rbrowser` | rBrowser web UI (port 5000) |
| `i2pd` | I2P daemon |

All services are `systemd` units with `Restart=always`.

---

## Reticulum config

FULL sets `enable_transport = No` — the gateway participates in the network but does not route packets for others. This is intentional: routing third-party traffic between interfaces creates legal ambiguity in some jurisdictions.

To add outbound TCP connections (required for Node Tracker to see remote nodes):

```ini
[[rmap.world]]
  type = TCPClientInterface
  enabled = yes
  target_host = rmap.world
  target_port = 4242
```

---

## Radio Observatory — API endpoints

| Endpoint | Description |
|---|---|
| `GET /api/rnode/history` | RSSI/SNR/Noise/Airtime ring buffer (20 min) |
| `GET /api/rnode/rf_stats` | Current RF metrics |
| `GET /api/rnode/activity` | RF packet + Noise activity log |
| `GET /api/rnode/interference` | Noise spike events |
| `GET /api/nodes` | Node tracker — all heard announces |
| `GET /api/nodes/debug` | Announce handler state |

---

## License

MIT
