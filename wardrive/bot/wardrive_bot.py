#!/opt/reticulum/bin/python3
"""
wardrive_bot.py - LoRa coverage recorder for Reticulum wardriving.

Listens for wardrive.beacon announces from the wardriving app.
Each announce carries an 8-byte GPS payload (big-endian float32 lat + lon).
Also accepts direct encrypted packets on the wardrive.collector destination
with a 24-byte payload (float32 lat + float32 lon + 16-byte sender hash).
Also accepts relay packets on the wardrive.aggregator destination from other
wardrive_bot instances forwarding their locally recorded points.
RSSI/SNR are read from the RNS packet cache via packet hash.
Requires a Reticulum instance already running; in NOEMA FULL the dashboard owns it.
"""

import os
import sys
import time
import struct
import json
import math
import random
import logging
import sqlite3
import datetime
import subprocess
import threading
from collections import deque

import RNS
import LXMF

APP_NAME     = "wardrive_bot"
APP_VERSION  = "0.1.0"
LOG_FILE     = "/etc/reticulum/wardrive_bot_logfile"
IDENTITY_DIR = "/etc/reticulum/storage/identities"
IDENTITY_PATH = os.path.join(IDENTITY_DIR, APP_NAME + "_identity")
STORAGE_PATH = f"/tmp/{APP_NAME}"

# Announce mode: 8-byte payload (lat + lon)
ANNOUNCE_PAYLOAD_FMT  = ">ff"
ANNOUNCE_PAYLOAD_SIZE = struct.calcsize(ANNOUNCE_PAYLOAD_FMT)

# Packet mode: 24-byte payload (lat + lon + 16-byte sender hash)
PACKET_PAYLOAD_FMT  = ">ff16s"
PACKET_PAYLOAD_SIZE = struct.calcsize(PACKET_PAYLOAD_FMT)

# Relay mode: forwarded to central aggregator. Includes downlink (bot-to-phone) metrics
# alongside the uplink ones so probe-mode relays carry both directions, not just uplink.
RELAY_PAYLOAD_FMT  = ">ffifi16sifi"
RELAY_PAYLOAD_SIZE = struct.calcsize(RELAY_PAYLOAD_FMT)
_INT_NULL          = 2147483647  # sentinel for nullable int32 fields

# Probe mode: 36-byte payload — lat + lon + sender hash + previous downlink metrics (N+1 lag)
# dl_rssi/_q use _INT_NULL sentinel when absent; dl_snr uses NaN.
PROBE_PAYLOAD_FMT  = ">ff16sifi"
PROBE_PAYLOAD_SIZE = struct.calcsize(PROBE_PAYLOAD_FMT)

# Probe reply: 12-byte uplink metrics sent back to the app
PROBE_REPLY_FMT  = ">ifi"
PROBE_REPLY_SIZE = struct.calcsize(PROBE_REPLY_FMT)

# Ping: 16-byte sender hash; reply is a UTF-8 text string sent to wardrive.ping_reply
PING_PAYLOAD_FMT  = ">16s"
PING_PAYLOAD_SIZE = struct.calcsize(PING_PAYLOAD_FMT)

# rnsd only auto-discovers a path to a destination if it happens to have heard a
# recent-enough announce for it — not guaranteed, and Packet.send() won't raise
# just because there's nowhere to send the packet. Used by both the ping/probe
# reply helper and the aggregator relay helper below.
REPLY_PATH_WAIT_SECS = 10

BOT_CONFIG_PATH = "/etc/reticulum/wardrive_bot.json"

def _load_bot_config():
    try:
        with open(BOT_CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[Warning] Could not load bot config from {BOT_CONFIG_PATH}: {e}")
        return {}

_bot_cfg         = _load_bot_config()
RELAY_HASH       = _bot_cfg.get("relay_hash",       None)
DISPLAY_NAME     = _bot_cfg.get("display_name",     "Bot [wardrive]")
# LXMF Field 4 (FIELD_ICON_APPEARANCE) — Sideband/Columba peer-icon interop.
# This is an LXMF-message-layer concept, not an RNS announce one: it rides on
# outbound LXMF replies (see make_reply()) and has no effect on the raw RNS
# announces/packets the wardriving traffic itself uses.
ICON_NAME        = _bot_cfg.get("icon_name",        "radio-tower")
ICON_FG_COLOR    = _bot_cfg.get("icon_fg_color",    "FFFFFF")  # Hex RGB
ICON_BG_COLOR    = _bot_cfg.get("icon_bg_color",    "4CAF50")  # Hex RGB
# Truncated to keep the announce payload small; carried as app_data on the
# collector announce so clients can show a friendly name instead of a hash.
COLLECTOR_APP_DATA = DISPLAY_NAME.encode("utf-8")[:64]
DB_PATH          = os.path.expanduser(_bot_cfg.get("db_path", "/etc/reticulum/wardrive_bot.db"))
ANNOUNCE_INTERVAL = int(_bot_cfg.get("announce_interval_secs", 3600))
NODES_CENTER_LAT  = _bot_cfg.get("initial_lat",     55.7558)
NODES_CENTER_LON  = _bot_cfg.get("initial_lon",     37.6173)
NODES_RADIUS_KM   = _bot_cfg.get("nodes_radius_km", 50)

# "announce" LXMF command: delay before reannouncing, and a throttle to keep
# it from being used to spam announces onto the network.
ANNOUNCE_CMD_DELAY_MIN_SECS = 15
ANNOUNCE_CMD_DELAY_MAX_SECS = 30
ANNOUNCE_CMD_MAX_REQUESTS   = 3
ANNOUNCE_CMD_WINDOW_SECS    = 15 * 60

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class RNSStyleFormatter(logging.Formatter):
    LEVEL_NAMES = {
        logging.DEBUG:    "Debug",
        logging.INFO:     "Info",
        logging.WARNING:  "Warning",
        logging.ERROR:    "Error",
        logging.CRITICAL: "Critical",
    }

    def format(self, record):
        ts    = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        level = f"[{self.LEVEL_NAMES.get(record.levelno, record.levelname)}]"
        return f"[{ts}] {level:<11}{record.getMessage()}"

def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = RNSStyleFormatter()

    try:
        fh = logging.FileHandler(LOG_FILE)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except PermissionError:
        fallback = os.path.expanduser(f"~/{APP_NAME}.log")
        fh = logging.FileHandler(fallback)
        fh.setFormatter(fmt)
        root.addHandler(fh)
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Warning]   Cannot write to {LOG_FILE}, logging to {fallback}")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

log = logging.getLogger(APP_NAME)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(path):
    con = sqlite3.connect(path, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS points (
            id            INTEGER PRIMARY KEY,
            ts            INTEGER NOT NULL,
            receiver_hash BLOB    NOT NULL,
            sender_hash   BLOB    NOT NULL,
            lat           INTEGER NOT NULL,
            lon           INTEGER NOT NULL,
            rssi          INTEGER,
            snr           REAL,
            q             INTEGER,
            source        TEXT,
            dl_rssi       INTEGER,
            dl_snr        REAL,
            dl_q          INTEGER
        )
    """)
    cols = [r[1] for r in con.execute("PRAGMA table_info(points)").fetchall()]
    for col, typedef in [("source", "TEXT"), ("dl_rssi", "INTEGER"), ("dl_snr", "REAL"), ("dl_q", "INTEGER")]:
        if col not in cols:
            con.execute(f"ALTER TABLE points ADD COLUMN {col} {typedef}")
    con.execute("CREATE INDEX IF NOT EXISTS points_ts ON points(ts)")
    con.commit()
    return con

def insert_point(con, receiver_hash, sender_hash, lat, lon, rssi, snr, q, source,
                 dl_rssi=None, dl_snr=None, dl_q=None):
    con.execute(
        "INSERT INTO points(ts, receiver_hash, sender_hash, lat, lon, rssi, snr, q, source, dl_rssi, dl_snr, dl_q) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (int(time.time()), receiver_hash, sender_hash,
         round(lat * 1_000_000), round(lon * 1_000_000),
         round(rssi) if rssi is not None else None,
         snr,
         round(q) if q is not None else None,
         source,
         round(dl_rssi) if dl_rssi is not None else None,
         dl_snr,
         round(dl_q) if dl_q is not None else None),
    )
    con.commit()

# ---------------------------------------------------------------------------
# Relay
# ---------------------------------------------------------------------------

def _send_relay_with_path(dest, payload, log_suffix, timeout=REPLY_PATH_WAIT_SECS):
    # Same silent-drop hazard as replies (see the reply helper further down):
    # identity known doesn't mean a path is known, and an aggregator outage long
    # enough for its path to expire elsewhere on the network left this sending
    # silently into the void, with a "RELAY-OUT ..." log line regardless of delivery.
    # has_path() being true doesn't mean the cached route still resolves — expire
    # and re-request unconditionally rather than only when has_path() is false,
    # or a stale-but-cached path never gets refreshed (see the reply helper).
    RNS.Transport.expire_path(dest.hash)
    RNS.Transport.request_path(dest.hash)
    waited = 0.0
    while not RNS.Transport.has_path(dest.hash) and waited < timeout:
        time.sleep(0.5)
        waited += 0.5

    if not RNS.Transport.has_path(dest.hash):
        log.warning(f"{'RELAY-OUT':<9} {log_suffix} — no path after requesting, giving up")
        return

    try:
        RNS.Packet(dest, payload).send()
        log.info(f"{'RELAY-OUT':<9} {log_suffix}")
    except Exception as e:
        log.warning(f"Relay failed: {e}")

def relay_point(lat, lon, rssi, snr, q, sender_hash, dl_rssi=None, dl_snr=None, dl_q=None):
    if not RELAY_HASH:
        return
    try:
        agg_hash = bytes.fromhex(RELAY_HASH)
        identity = RNS.Identity.recall(agg_hash)
        if identity is None:
            RNS.Transport.request_path(agg_hash)
            log.warning("Relay: aggregator identity unknown, path requested")
            return
        rssi_val    = round(rssi)    if rssi    is not None else _INT_NULL
        snr_val     = float(snr)     if snr     is not None else float('nan')
        q_val       = round(q)       if q       is not None else _INT_NULL
        dl_rssi_val = round(dl_rssi) if dl_rssi is not None else _INT_NULL
        dl_snr_val  = float(dl_snr)  if dl_snr  is not None else float('nan')
        dl_q_val    = round(dl_q)    if dl_q    is not None else _INT_NULL
        payload  = struct.pack(
            RELAY_PAYLOAD_FMT, lat, lon, rssi_val, snr_val, q_val, bytes(sender_hash),
            dl_rssi_val, dl_snr_val, dl_q_val,
        )
        dest     = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "wardrive", "aggregator")
        rel_parts = [f"Lat: {lat:.5f}", f"Lon: {lon:.5f}"]
        if rssi    is not None: rel_parts.append(f"RSSI: {round(rssi)}dBm")
        if snr     is not None: rel_parts.append(f"SNR: {snr}dB")
        if q       is not None: rel_parts.append(f"Q: {round(q)}")
        if dl_rssi is not None: rel_parts.append(f"DL-RSSI: {round(dl_rssi)}dBm")
        if dl_snr  is not None: rel_parts.append(f"DL-SNR: {dl_snr}dB")
        log_suffix = f"dst={RNS.prettyhexrep(agg_hash)}  [{', '.join(rel_parts)}]"
        threading.Thread(
            target=_send_relay_with_path,
            args=(dest, payload, log_suffix),
            daemon=True,
        ).start()
    except Exception as e:
        log.warning(f"Relay failed: {e}")

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def load_or_create_identity():
    os.makedirs(IDENTITY_DIR, exist_ok=True)

    if os.path.exists(IDENTITY_PATH):
        identity = RNS.Identity.from_file(IDENTITY_PATH)
    else:
        identity = RNS.Identity()
        identity.to_file(IDENTITY_PATH)
        log.info(f"Generated new identity, saved to {IDENTITY_PATH}")

    return identity

# ---------------------------------------------------------------------------
# Announce state — shared between the periodic announce loop in main() and
# the on-demand "announce" LXMF command, so a command-triggered reannounce
# also resets the periodic timer instead of announcing twice in short order.
# ---------------------------------------------------------------------------

class AnnounceState:
    def __init__(self, lxmf_dest, collector_dest, probe_dest, ping_dest, aggregator_dest):
        self.lxmf_dest      = lxmf_dest
        self.collector_dest = collector_dest
        self.probe_dest     = probe_dest
        self.ping_dest      = ping_dest
        self.aggregator_dest = aggregator_dest
        self.lock           = threading.Lock()
        self.last_announce  = 0.0

    def announce_all(self):
        with self.lock:
            self.lxmf_dest.announce()
            self.collector_dest.announce(app_data=COLLECTOR_APP_DATA)
            self.probe_dest.announce()
            self.ping_dest.announce()
            if self.aggregator_dest:
                self.aggregator_dest.announce()
            self.last_announce = time.time()

            dests = (
                f"lxmf={RNS.prettyhexrep(self.lxmf_dest.hash)}"
                f" collector={RNS.prettyhexrep(self.collector_dest.hash)}"
                f" probe={RNS.prettyhexrep(self.probe_dest.hash)}"
                f" ping={RNS.prettyhexrep(self.ping_dest.hash)}"
            )
            if self.aggregator_dest:
                dests += f" aggregator={RNS.prettyhexrep(self.aggregator_dest.hash)}"
            log.info(f"{'ANNOUNCE':<9} {dests}")

# ---------------------------------------------------------------------------
# LXMF command handler
# ---------------------------------------------------------------------------

_NODE_TYPE_LABELS = {
    "RNodeInterface":    "RNode",
    "BackboneInterface": "Backbone",
    "I2PInterface":      "I2P",
}

def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def nodes_near_center():
    try:
        result = subprocess.run(
            ["rnstatus", "-d", "-j"],
            capture_output=True, text=True, timeout=15
        )
        raw = json.loads(result.stdout)
    except Exception as e:
        log.warning(f"nodes cmd: rnstatus failed: {e}")
        return None

    counts = {}
    for d in raw:
        rtype = d.get("type", "")
        if rtype not in _NODE_TYPE_LABELS:
            continue
        lat = d.get("latitude")
        lon = d.get("longitude")
        if lat is None or lon is None:
            continue
        if _haversine_km(NODES_CENTER_LAT, NODES_CENTER_LON, lat, lon) > NODES_RADIUS_KM:
            continue
        label = _NODE_TYPE_LABELS[rtype]
        counts[label] = counts.get(label, 0) + 1

    return counts


_DEFAULT_WELCOME_MSG = (
    f"👋 {DISPLAY_NAME} here.\n"
    "I record LoRa RNode coverage from announces and direct packets.\n\n"
    "Available commands:\n"
    "  ping     — pings the bot, provides RF metrics\n"
    "  status   — point count and last seen timestamp\n"
    f"  nodes    — discoverable nodes within {NODES_RADIUS_KM}km\n"
    "  destinations — list this bot's destination hashes\n"
    "  announce — reannounce all bot destinations\n\n"
)
WELCOME_MSG = _bot_cfg.get("welcome_message") or _DEFAULT_WELCOME_MSG

# [icon_name, fg_bytes, bg_bytes] per LXMF's Field 4 convention (raw 3-byte RGB,
# not hex strings, on the wire). None disables the field if the configured hex
# is malformed, rather than crashing the bot over a cosmetic feature.
try:
    _ICON_APPEARANCE_FIELD = [ICON_NAME, bytes.fromhex(ICON_FG_COLOR), bytes.fromhex(ICON_BG_COLOR)]
except Exception as e:
    print(f"[Warning] Invalid icon_fg_color/icon_bg_color hex, disabling icon appearance: {e}")
    _ICON_APPEARANCE_FIELD = None

def make_reply(router, lxmf_dest, message, text):
    fields = {LXMF.FIELD_ICON_APPEARANCE: _ICON_APPEARANCE_FIELD} if _ICON_APPEARANCE_FIELD else None
    reply = LXMF.LXMessage(
        message.source,
        lxmf_dest,
        text,
        fields=fields,
        desired_method=LXMF.LXMessage.DIRECT,
    )
    router.handle_outbound(reply)

def handle_lxmf(router, lxmf_dest, db, announce_state):
    # Throttle state for the "announce" command — module-private to this
    # closure since handle_lxmf() is only ever called once, from main().
    announce_request_times = deque()
    announce_throttle_lock = threading.Lock()

    def do_delayed_announce(delay, message, src):
        time.sleep(delay)
        announce_state.announce_all()

        now = time.time()
        with announce_throttle_lock:
            while announce_request_times and now - announce_request_times[0] > ANNOUNCE_CMD_WINDOW_SECS:
                announce_request_times.popleft()
            remaining = max(0, ANNOUNCE_CMD_MAX_REQUESTS - len(announce_request_times))

        text = f"Reannounce done. {remaining} request(s) remaining in this 15-minute window."
        make_reply(router, lxmf_dest, message, text)
        log.info(f"{'LXMF TX':<9} dst={src}")

    def delivery(message):
        src = RNS.prettyhexrep(message.source.hash)
        cmd = (message.content or b"").decode("utf-8", errors="replace").strip().lower()
        log.info(f"{'LXMF RX':<9} src={src}  cmd={cmd!r}")

        if cmd == "announce":
            now = time.time()
            with announce_throttle_lock:
                while announce_request_times and now - announce_request_times[0] > ANNOUNCE_CMD_WINDOW_SECS:
                    announce_request_times.popleft()
                if len(announce_request_times) >= ANNOUNCE_CMD_MAX_REQUESTS:
                    retry_in = int(ANNOUNCE_CMD_WINDOW_SECS - (now - announce_request_times[0]))
                    text = f"Too many announce requests, try again in {retry_in}s."
                    make_reply(router, lxmf_dest, message, text)
                    log.info(f"{'LXMF TX':<9} dst={src}  (announce throttled, retry in {retry_in}s)")
                    return
                announce_request_times.append(now)

            delay = random.uniform(ANNOUNCE_CMD_DELAY_MIN_SECS, ANNOUNCE_CMD_DELAY_MAX_SECS)
            log.info(f"{'ANNOUNCE':<9} src={src}  reannounce requested, scheduled in {delay:.1f}s")
            threading.Thread(
                target=do_delayed_announce,
                args=(delay, message, src),
                daemon=True,
            ).start()
            return
        elif cmd == "ping":
            now_str = datetime.datetime.now().strftime("%H:%M:%S")
            rf = [now_str]
            rssi = getattr(message, "rssi", None)
            snr  = getattr(message, "snr",  None)
            if rssi is not None: rf.append(f"RSSI: {round(rssi)}dBm")
            if snr  is not None: rf.append(f"SNR: {snr}dB")
            if rssi is None and snr is None:
                hops = RNS.Transport.hops_to(message.source.hash)
                if hops != RNS.Transport.PATHFINDER_M:
                    rf.append(f"Hops: {hops}")
            text = f"Pong! [{', '.join(rf)}]"
        elif cmd == "status":
            row = db.execute("SELECT COUNT(*), MAX(ts) FROM points").fetchone()
            count, last_ts = row
            if last_ts:
                last_str = datetime.datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M:%S")
            else:
                last_str = "never"
            text = f"Points recorded: {count}\nLast beacon: {last_str}"
        elif cmd == "nodes":
            counts = nodes_near_center()
            if counts is None:
                text = "Could not query rnstatus."
            elif not counts:
                text = f"No discoverable nodes within {NODES_RADIUS_KM}km."
            else:
                total = sum(counts.values())
                breakdown = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
                text = f"Nodes within {NODES_RADIUS_KM}km: {total}\n({breakdown})"
        elif cmd == "destinations":
            lines = [
                f"Collector destination hash: {RNS.prettyhexrep(announce_state.collector_dest.hash)}",
                f"Probe destination hash: {RNS.prettyhexrep(announce_state.probe_dest.hash)}",
            ]
            if announce_state.aggregator_dest:
                lines.append(
                    f"Relay destination hash: {RNS.prettyhexrep(announce_state.aggregator_dest.hash)}"
                )
            text = "\n".join(lines)
        else:
            text = WELCOME_MSG

        make_reply(router, lxmf_dest, message, text)
        log.info(f"{'LXMF TX':<9} dst={src}")

    return delivery

# ---------------------------------------------------------------------------
# Announce handler (wardrive.beacon)
# ---------------------------------------------------------------------------

class WardrivAnnounceHandler:
    aspect_filter = "wardrive.beacon"

    def __init__(self, reticulum, db, receiver_hash):
        self.reticulum     = reticulum
        self.db            = db
        self.receiver_hash = receiver_hash

    def received_announce(self, destination_hash, announced_identity,
                          app_data, announce_packet_hash):
        rssi = self.reticulum.get_packet_rssi(announce_packet_hash)
        snr  = self.reticulum.get_packet_snr(announce_packet_hash)
        q    = self.reticulum.get_packet_q(announce_packet_hash)

        source_hex = RNS.prettyhexrep(destination_hash)

        if rssi is None and snr is None:
            log.warning(f"Dropped announce {source_hex}: no RF stats (not LoRa)")
            return

        if app_data is None or len(app_data) != ANNOUNCE_PAYLOAD_SIZE:
            size = len(app_data) if app_data else 0
            log.warning(f"Dropped announce {source_hex}: bad app_data ({size}B, expected {ANNOUNCE_PAYLOAD_SIZE}B)")
            return

        try:
            lat, lon = struct.unpack(ANNOUNCE_PAYLOAD_FMT, app_data)
        except struct.error as e:
            log.warning(f"Dropped announce {source_hex}: unpack failed — {e}")
            return

        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            log.warning(f"Dropped announce {source_hex}: invalid coordinates ({lat}, {lon})")
            return

        parts = [f"Lat: {lat:.5f}", f"Lon: {lon:.5f}"]
        if rssi is not None: parts.append(f"RSSI: {rssi}dBm")
        if snr  is not None: parts.append(f"SNR: {snr} dB")
        if q    is not None: parts.append(f"Q: {round(q)}")

        log.info(f"{'ANNOUNCE':<9} src={source_hex}  [{', '.join(parts)}]")
        insert_point(self.db, self.receiver_hash, bytes(destination_hash),
                     lat, lon, rssi, snr, q, "ann")
        relay_point(lat, lon, rssi, snr, q, bytes(destination_hash))

# ---------------------------------------------------------------------------
# Packet handler (wardrive.collector)
# ---------------------------------------------------------------------------

def handle_collector_packet(reticulum, db, receiver_hash):
    def callback(message, packet):
        rssi = reticulum.get_packet_rssi(packet.packet_hash)
        snr  = reticulum.get_packet_snr(packet.packet_hash)
        q    = reticulum.get_packet_q(packet.packet_hash)

        if rssi is None and snr is None:
            log.warning("Dropped collector packet: no RF stats (not LoRa)")
            return

        if len(message) != PACKET_PAYLOAD_SIZE:
            log.warning(f"Dropped collector packet: bad size ({len(message)}B, expected {PACKET_PAYLOAD_SIZE}B)")
            return

        try:
            lat, lon, sender_hash = struct.unpack(PACKET_PAYLOAD_FMT, message)
        except struct.error as e:
            log.warning(f"Dropped collector packet: unpack failed — {e}")
            return

        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            log.warning(f"Dropped collector packet: invalid coordinates ({lat}, {lon})")
            return

        source_hex = RNS.prettyhexrep(sender_hash)
        parts = [f"Lat: {lat:.5f}", f"Lon: {lon:.5f}"]
        if rssi is not None: parts.append(f"RSSI: {round(rssi)}dBm")
        if snr  is not None: parts.append(f"SNR: {snr}dB")
        if q    is not None: parts.append(f"Q: {round(q)}")
        log.info(f"{'PACKET':<9} src={source_hex}  [{', '.join(parts)}]")
        insert_point(db, receiver_hash, sender_hash, lat, lon, rssi, snr, q, "pkt")
        relay_point(lat, lon, rssi, snr, q, sender_hash)

    return callback

# ---------------------------------------------------------------------------
# Reply helper — request a path if we don't already have one
# ---------------------------------------------------------------------------
#
# rnsd only auto-discovers a path to a client's reply destination if it happens
# to have heard a recent-enough announce for it. That's not guaranteed — paths
# can go missing (observed via a diagnostic has_path()/hops_to() check) well
# before their nominal TTL, with no error anywhere: Packet.send() doesn't raise
# just because there's nowhere to send the packet. Unlike an announce, a reply
# is a one-shot unicast send with no retry, so a missing path silently eats it.
# Mirror what the wardrive app itself does when it wants to reach the bot:
# explicitly request a path and wait briefly before giving up. Also expire the
# cached path unconditionally first — has_path() being true doesn't mean the
# route still resolves (e.g. the sender's link just changed interfaces, as when
# switching a client from a TCP uplink to RNode mid-session): request_path() is
# a no-op while has_path() is true, so a stale-but-cached path masquerading as
# valid would otherwise never get refreshed, and the reply gets sent into the
# void with a "PONG ..."/"PROBE-OUT ..." log line regardless of delivery.
# (REPLY_PATH_WAIT_SECS is defined up near the payload format constants, shared
# with the aggregator relay helper.)

def _send_reply_with_path(dest, payload, kind, source_hex, log_detail=None, timeout=REPLY_PATH_WAIT_SECS):
    RNS.Transport.expire_path(dest.hash)
    RNS.Transport.request_path(dest.hash)
    waited = 0.0
    while not RNS.Transport.has_path(dest.hash) and waited < timeout:
        time.sleep(0.5)
        waited += 0.5

    if not RNS.Transport.has_path(dest.hash):
        log.warning(f"{kind:<9} dst={source_hex}  — no path after requesting, giving up")
        return

    try:
        RNS.Packet(dest, payload).send()
        suffix = f"  {log_detail}" if log_detail else ""
        log.info(f"{kind:<9} dst={source_hex}{suffix}")
    except Exception as e:
        log.warning(f"{kind:<9} dst={source_hex}  — send failed: {e}")

def send_reply_async(dest, payload, kind, source_hex, log_detail=None, timeout=REPLY_PATH_WAIT_SECS):
    threading.Thread(
        target=_send_reply_with_path,
        args=(dest, payload, kind, source_hex, log_detail, timeout),
        daemon=True,
    ).start()

# ---------------------------------------------------------------------------
# Probe packet handler (wardrive.probe) — bidirectional mode
# ---------------------------------------------------------------------------

def handle_probe_packet(reticulum, db, receiver_hash):
    def callback(message, packet):
        rssi = reticulum.get_packet_rssi(packet.packet_hash)
        snr  = reticulum.get_packet_snr(packet.packet_hash)
        q    = reticulum.get_packet_q(packet.packet_hash)

        if rssi is None and snr is None:
            log.warning("Dropped probe packet: no RF stats (not LoRa)")
            return

        if len(message) != PROBE_PAYLOAD_SIZE:
            log.warning(f"Dropped probe packet: bad size ({len(message)}B, expected {PROBE_PAYLOAD_SIZE}B)")
            return

        try:
            lat, lon, sender_hash, dl_rssi_val, dl_snr_val, dl_q_val = struct.unpack(PROBE_PAYLOAD_FMT, message)
        except struct.error as e:
            log.warning(f"Dropped probe packet: unpack failed — {e}")
            return

        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            log.warning(f"Dropped probe packet: invalid coordinates ({lat}, {lon})")
            return

        dl_rssi = None if dl_rssi_val == _INT_NULL else dl_rssi_val
        dl_snr  = None if math.isnan(dl_snr_val)  else float(dl_snr_val)
        dl_q    = None if dl_q_val   == _INT_NULL else dl_q_val

        parts = [f"Lat: {lat:.5f}", f"Lon: {lon:.5f}"]
        if rssi    is not None: parts.append(f"RSSI: {rssi}dBm")
        if snr     is not None: parts.append(f"SNR: {snr} dB")
        if q       is not None: parts.append(f"Q: {round(q)}")
        if dl_rssi is not None: parts.append(f"DL-RSSI: {dl_rssi}dBm")
        if dl_snr  is not None: parts.append(f"DL-SNR: {dl_snr} dB")

        source_hex = RNS.prettyhexrep(sender_hash)
        log.info(f"{'PROBE-IN':<9} src={source_hex}  [{', '.join(parts)}]")
        insert_point(db, receiver_hash, sender_hash, lat, lon, rssi, snr, q, "probe",
                     dl_rssi=dl_rssi, dl_snr=dl_snr, dl_q=dl_q)
        relay_point(lat, lon, rssi, snr, q, sender_hash, dl_rssi=dl_rssi, dl_snr=dl_snr, dl_q=dl_q)

        # Reply with bot-side uplink metrics to sender's wardrive.probe_reply destination
        sender_identity = RNS.Identity.recall(sender_hash)
        if sender_identity is None:
            log.warning(f"Probe reply: unknown identity for {source_hex}, skipping reply")
            return
        reply_dest = RNS.Destination(
            sender_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "wardrive", "probe_reply",
        )
        rssi_val = round(rssi) if rssi is not None else _INT_NULL
        snr_val  = float(snr)  if snr  is not None else float("nan")
        q_val    = round(q)    if q    is not None else _INT_NULL
        reply_payload = struct.pack(PROBE_REPLY_FMT, rssi_val, snr_val, q_val)
        reply_parts = []
        if rssi is not None: reply_parts.append(f"RSSI: {rssi}dBm")
        if snr  is not None: reply_parts.append(f"SNR: {snr} dB")
        if q    is not None: reply_parts.append(f"Q: {round(q)}")
        send_reply_async(reply_dest, reply_payload, "PROBE-OUT", source_hex,
                          log_detail=f"[{', '.join(reply_parts)}]")

    return callback

# ---------------------------------------------------------------------------
# Ping packet handler (wardrive.ping)
# ---------------------------------------------------------------------------

def handle_ping_packet(reticulum):
    def callback(message, packet):
        rssi = reticulum.get_packet_rssi(packet.packet_hash)
        snr  = reticulum.get_packet_snr(packet.packet_hash)
        q    = reticulum.get_packet_q(packet.packet_hash)

        if len(message) != PING_PAYLOAD_SIZE:
            log.warning(f"Dropped ping packet: bad size ({len(message)}B, expected {PING_PAYLOAD_SIZE}B)")
            return

        try:
            sender_hash, = struct.unpack(PING_PAYLOAD_FMT, message)
        except struct.error as e:
            log.warning(f"Dropped ping packet: unpack failed — {e}")
            return

        source_hex = RNS.prettyhexrep(sender_hash)
        parts = [datetime.datetime.now().strftime("%H:%M:%S")]
        if rssi is not None: parts.append(f"RSSI: {round(rssi)}dBm")
        if snr  is not None: parts.append(f"SNR: {snr}dB")
        if q    is not None: parts.append(f"Q: {round(q)}")
        if rssi is None and snr is None:
            hops = RNS.Transport.hops_to(sender_hash)
            if hops != RNS.Transport.PATHFINDER_M:
                parts.append(f"Hops: {hops}")
        text = f"Pong! [{', '.join(parts)}]"
        log.info(f"{'PING':<9} src={source_hex}")

        sender_identity = RNS.Identity.recall(sender_hash)
        if sender_identity is None:
            log.warning(f"Ping reply: unknown identity for {source_hex}")
            return
        reply_dest = RNS.Destination(
            sender_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "wardrive", "ping_reply",
        )
        send_reply_async(reply_dest, text.encode("utf-8"), "PONG", source_hex, log_detail=text)

    return callback

# ---------------------------------------------------------------------------
# Relay packet handler (wardrive.aggregator)
# ---------------------------------------------------------------------------

def handle_relay_packet(reticulum, db, receiver_hash):
    def callback(message, packet):
        if len(message) != RELAY_PAYLOAD_SIZE:
            log.warning(f"Dropped relay packet: bad size ({len(message)}B, expected {RELAY_PAYLOAD_SIZE}B)")
            return

        try:
            lat, lon, rssi_val, snr_val, q_val, sender_hash, dl_rssi_val, dl_snr_val, dl_q_val = \
                struct.unpack(RELAY_PAYLOAD_FMT, message)
        except struct.error as e:
            log.warning(f"Dropped relay packet: unpack failed — {e}")
            return

        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            log.warning(f"Dropped relay packet: invalid coordinates ({lat}, {lon})")
            return

        rssi    = None if rssi_val    == _INT_NULL else rssi_val
        snr     = None if math.isnan(snr_val)      else float(snr_val)
        q       = None if q_val       == _INT_NULL else q_val
        dl_rssi = None if dl_rssi_val == _INT_NULL else dl_rssi_val
        dl_snr  = None if math.isnan(dl_snr_val)   else float(dl_snr_val)
        dl_q    = None if dl_q_val    == _INT_NULL else dl_q_val

        parts = [f"Lat: {lat:.5f}", f"Lon: {lon:.5f}"]
        if rssi    is not None: parts.append(f"RSSI: {rssi}dBm")
        if snr     is not None: parts.append(f"SNR: {snr} dB")
        if q       is not None: parts.append(f"Q: {round(q)}")
        if dl_rssi is not None: parts.append(f"DL-RSSI: {dl_rssi}dBm")
        if dl_snr  is not None: parts.append(f"DL-SNR: {dl_snr} dB")

        source_hex = RNS.prettyhexrep(sender_hash)
        log.info(f"{'RELAY-IN':<9} src={source_hex}  [{', '.join(parts)}]")
        insert_point(db, receiver_hash, bytes(sender_hash), lat, lon, rssi, snr, q, "relay",
                     dl_rssi=dl_rssi, dl_snr=dl_snr, dl_q=dl_q)

    return callback

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    setup_logging()
    log.info(f"Started {APP_NAME} {APP_VERSION}")

    try:
        # NOEMA FULL owns the physical Reticulum instance in dashboard.service.
        # Reticulum attaches to that local shared instance using the same config.
        reticulum = RNS.Reticulum(configdir=os.environ.get(
            "RNS_CONFIG_DIR", os.path.expanduser("~/.reticulum")))
    except Exception as e:
        log.error(f"Failed to connect to Reticulum: {e}")
        sys.exit(1)

    log.info("Connected to shared RNS instance")

    identity = load_or_create_identity()
    db = init_db(DB_PATH)

    router    = LXMF.LXMRouter(storagepath=STORAGE_PATH)
    lxmf_dest = router.register_delivery_identity(identity, display_name=DISPLAY_NAME)

    collector_dest = RNS.Destination(
        identity,
        RNS.Destination.IN,
        RNS.Destination.SINGLE,
        "wardrive", "collector",
    )
    collector_dest.set_packet_callback(
        handle_collector_packet(reticulum, db, bytes(lxmf_dest.hash))
    )

    probe_dest = RNS.Destination(
        identity,
        RNS.Destination.IN,
        RNS.Destination.SINGLE,
        "wardrive", "probe",
    )
    probe_dest.set_packet_callback(
        handle_probe_packet(reticulum, db, bytes(lxmf_dest.hash))
    )

    ping_dest = RNS.Destination(
        identity,
        RNS.Destination.IN,
        RNS.Destination.SINGLE,
        "wardrive", "ping",
    )
    ping_dest.set_packet_callback(handle_ping_packet(reticulum))

    if not RELAY_HASH:
        aggregator_dest = RNS.Destination(
            identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            "wardrive", "aggregator",
        )
        aggregator_dest.set_packet_callback(
            handle_relay_packet(reticulum, db, bytes(lxmf_dest.hash))
        )
    else:
        aggregator_dest = None

    announce_state = AnnounceState(lxmf_dest, collector_dest, probe_dest, ping_dest, aggregator_dest)
    router.register_delivery_callback(handle_lxmf(router, lxmf_dest, db, announce_state))

    handler = WardrivAnnounceHandler(reticulum, db, bytes(lxmf_dest.hash))
    RNS.Transport.register_announce_handler(handler)

    log.info(f"Name: \"{DISPLAY_NAME}\"")
    log.info(f"LXMF Address         : {RNS.prettyhexrep(lxmf_dest.hash)}")
    log.info(f"Collector Destination: {RNS.prettyhexrep(collector_dest.hash)}")
    log.info(f"Probe Destination    : {RNS.prettyhexrep(probe_dest.hash)}")
    log.info(f"Ping Destination     : {RNS.prettyhexrep(ping_dest.hash)}")
    if aggregator_dest:
        log.info(f"Aggregator Destination: {RNS.prettyhexrep(aggregator_dest.hash)}")

    while True:
        if time.time() - announce_state.last_announce >= ANNOUNCE_INTERVAL:
            announce_state.announce_all()
        time.sleep(1)

if __name__ == "__main__":
    main()
