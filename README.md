# NOEMA RNSGate FULL

**Полнофункциональный шлюз Reticulum/LXMF с дашбордом Radio Observatory**

FULL — это расширенная версия [NOEMA RNSGate Lite](https://github.com/e2ret/NOEMA-RNSGate-Lite). Ключевое архитектурное отличие: дашборд сам владеет инстансом Reticulum напрямую, что открывает доступ к реальной RF-телеметрии от RNode интерфейсов.

[🇷🇺 Русский](#русский) | [🇬🇧 English](#english)

---

## Русский

### Ключевые отличия от Lite

| | Lite | FULL |
|---|---|---|
| Владелец Reticulum | `rnsd` (отдельный процесс) | `dashboard` (Flask app) |
| RSSI/SNR | ❌ всегда null | ✅ реальные значения |
| История RF телеметрии | ❌ | ✅ |
| Node Tracker | ❌ | ✅ |
| RF Activity Log | ❌ | ✅ |
| Детекция помех | ❌ | ✅ |
| Карта сети (dartboard) | ❌ | ✅ |
| PTY Терминал | ❌ | ✅ |

### Возможности

- **Radio Observatory** — графики RSSI/SNR/Noise/Airtime в реальном времени от RNodeInterface
- **RF Activity Log** — каждый принятый LoRa пакет с уровнем сигнала, RSSI, SNR; изменения шума
- **Interference Events** — автоматическая детекция скачков шума (>3 dBm выше базового уровня)
- **Node Tracker** — все узлы Reticulum слышимые через announce, dartboard карта сети с кольцами хопов
- **PTY Терминал** — полноценный bash терминал прямо в браузере (вкладка Logs)
- **LXMF Чат** — отправка/приём сообщений через MQTT bridge
- **Nomadnet** — браузер страниц и хостинг узла
- **I2P интеграция** — анонимный входящий туннель через i2pd
- **Панель системы** — управление сервисами, редактор конфига Reticulum, backup/restore

### Железо

- Любой Linux сервер/SBC (протестировано на x86-64 и ARM)
- RNode (LoRa) подключённый через TCP или USB — опционально, но необходим для RF телеметрии

### Быстрая установка

```bash
git clone https://github.com/e2ret/NOEMA-RNSGate-FULL.git
cd NOEMA-RNSGate-FULL
sudo bash install.sh
```

После установки откройте `http://<ip-адрес>:8081`

### Сервисы

| Сервис | Описание |
|---|---|
| `dashboard` | Основное Flask приложение — владелец Reticulum |
| `noema_lxmf_bridge` | LXMF ↔ MQTT bridge |
| `nomadnet` | Nomadnet демон |
| `rbrowser` | rBrowser веб-интерфейс (порт 5000) |
| `i2pd` | I2P демон |

Все сервисы — `systemd` юниты с `Restart=always`.

### Конфиг Reticulum

FULL устанавливает `enable_transport = No` — шлюз участвует в сети но не маршрутизирует чужие пакеты. Это сделано намеренно: маршрутизация стороннего трафика между интерфейсами создаёт правовую неоднозначность в ряде юрисдикций.

Для добавления исходящих TCP соединений (необходимо для Node Tracker):

```ini
[[rmap.world]]
  type = TCPClientInterface
  enabled = yes
  target_host = rmap.world
  target_port = 4242
```

### API Radio Observatory

| Endpoint | Описание |
|---|---|
| `GET /api/rnode/history` | Кольцевой буфер RSSI/SNR/Noise/Airtime (20 мин) |
| `GET /api/rnode/rf_stats` | Текущие RF метрики |
| `GET /api/rnode/activity` | Лог RF пакетов и изменений шума |
| `GET /api/rnode/interference` | События скачков шума |
| `GET /api/nodes` | Node tracker — все услышанные announces |
| `GET /api/nodes/debug` | Состояние обработчика announces |

---
## Поддержать проект
Если проект полезен, буду рад поддержке.

| Сеть | Адрес |
|------|-------|
| **USDT TRC20** | `TD89XXL9ehwhp4WysfqHSBGJjxxdoaVsYD` |
| **TON** | `UQCmROnKeaWIt5Uxu3MTebKUjYQHuvVeyOauWdVn6srUWKX8` |

## English

**Full-featured Reticulum/LXMF gateway with Radio Observatory dashboard**

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

| Network | Address |
|---------|---------|
| **USDT TRC20** | `TD89XXL9ehwhp4WysfqHSBGJjxxdoaVsYD` |
| **TON** | `UQCmROnKeaWIt5Uxu3MTebKUjYQHuvVeyOauWdVn6srUWKX8` |
