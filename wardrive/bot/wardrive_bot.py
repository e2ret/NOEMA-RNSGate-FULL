#!/usr/bin/env python3
"""
wardrive_bot.py — LoRa coverage recorder for NOEMA Beacon.

Listens only for wardrive.beacon announces (8-byte big-endian float32 lat+lon).
Writes points into the SQLite DB that wardrive_map reads.
Requires a Reticulum instance already running; in NOEMA FULL the dashboard owns it.

No LXMF chat, collector, probe, ping, or aggregator destinations.
"""

import os
import sys
import time
import struct
import json
import logging
import sqlite3

import RNS

APP_NAME      = "wardrive_bot"
APP_VERSION   = "0.2.0"
IDENTITY_DIR  = "/etc/reticulum/storage/identities"
IDENTITY_PATH = os.path.join(IDENTITY_DIR, APP_NAME + "_identity")
BOT_CONFIG_PATH = "/etc/reticulum/wardrive_bot.json"

ANNOUNCE_PAYLOAD_FMT  = ">ff"
ANNOUNCE_PAYLOAD_SIZE = struct.calcsize(ANNOUNCE_PAYLOAD_FMT)


def _load_bot_config():
    try:
        with open(BOT_CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[Warning] Could not load bot config from {BOT_CONFIG_PATH}: {e}")
        return {}


_bot_cfg     = _load_bot_config()
DISPLAY_NAME = _bot_cfg.get("display_name", "NOEMA Wardrive")
DB_PATH      = os.path.expanduser(_bot_cfg.get("db_path", "/etc/reticulum/wardrive_bot.db"))


class RNSStyleFormatter(logging.Formatter):
    LEVEL_NAMES = {
        logging.DEBUG:    "Debug",
        logging.INFO:     "Info",
        logging.WARNING:  "Warning",
        logging.ERROR:    "Error",
        logging.CRITICAL: "Critical",
    }

    def format(self, record):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        level = self.LEVEL_NAMES.get(record.levelno, record.levelname)
        return f"[{ts}] [{level:<7}] {record.getMessage()}"


def setup_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RNSStyleFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger(APP_NAME).setLevel(logging.INFO)


log = logging.getLogger(APP_NAME)


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


def insert_point(con, receiver_hash, sender_hash, lat, lon, rssi, snr, q):
    con.execute(
        "INSERT INTO points(ts, receiver_hash, sender_hash, lat, lon, rssi, snr, q, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (int(time.time()), receiver_hash, sender_hash,
         round(lat * 1_000_000), round(lon * 1_000_000),
         round(rssi) if rssi is not None else None,
         snr,
         round(q) if q is not None else None,
         "ann"),
    )
    con.commit()


def load_or_create_identity():
    os.makedirs(IDENTITY_DIR, exist_ok=True)
    if os.path.exists(IDENTITY_PATH):
        return RNS.Identity.from_file(IDENTITY_PATH)
    identity = RNS.Identity()
    identity.to_file(IDENTITY_PATH)
    log.info(f"Generated new identity, saved to {IDENTITY_PATH}")
    return identity


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
            log.warning(
                f"Dropped announce {source_hex}: bad app_data "
                f"({size}B, expected {ANNOUNCE_PAYLOAD_SIZE}B)"
            )
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
        if rssi is not None:
            parts.append(f"RSSI: {rssi}dBm")
        if snr is not None:
            parts.append(f"SNR: {snr} dB")
        if q is not None:
            parts.append(f"Q: {round(q)}")

        log.info(f"{'BEACON':<9} src={source_hex}  [{', '.join(parts)}]")
        insert_point(self.db, self.receiver_hash, bytes(destination_hash),
                     lat, lon, rssi, snr, q)


def main():
    setup_logging()
    log.info(f"Started {APP_NAME} {APP_VERSION}")

    try:
        reticulum = RNS.Reticulum(configdir=os.environ.get(
            "RNS_CONFIG_DIR", os.path.expanduser("~/.reticulum")))
    except Exception as e:
        log.error(f"Failed to connect to Reticulum: {e}")
        sys.exit(1)

    log.info("Connected to shared RNS instance")

    identity = load_or_create_identity()
    db = init_db(DB_PATH)
    receiver_hash = bytes(identity.hash)

    RNS.Transport.register_announce_handler(
        WardrivAnnounceHandler(reticulum, db, receiver_hash)
    )

    log.info(f'Name: "{DISPLAY_NAME}"')
    log.info(f"Listening for wardrive.beacon → {DB_PATH}")
    log.info(f"Receiver identity: {RNS.prettyhexrep(identity.hash)}")

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
