import os
import json
import math
import struct
import threading
import time

import RNS

CONFIG_FILENAME = "wardrive_config.json"
MIN_ANNOUNCE_INTERVAL = 15   # minimum auto-mode announce interval in seconds
MIN_ANNOUNCE_DISTANCE = 10   # minimum movement in meters before a new announce is sent (auto mode only)

# Announce mode payload: lat + lon
ANNOUNCE_PAYLOAD_FMT = ">ff"

# Packet mode payload: lat + lon + 16-byte sender hash
PACKET_PAYLOAD_FMT  = ">ff16s"


class WardrivPlugin(SidebandServicePlugin):
    service_name = "wardrive"

    def start(self):
        self._config      = {}
        self._beacon_dest = None
        self._last_send   = 0
        self._trigger     = threading.Event()
        self._patched     = False
        self._last_lat    = None
        self._last_lon    = None

        try:
            sb   = self.get_sideband()
            path = os.path.join(sb.config.get("command_plugins_path", ""), CONFIG_FILENAME)
            with open(path) as f:
                self._config = json.load(f)
            RNS.log(f"[wardrive] config: interval={self._interval()}s mode={self._mode()} send={self._send_mode()}", RNS.LOG_NOTICE)
        except FileNotFoundError:
            RNS.log("[wardrive] no config file, using defaults", RNS.LOG_NOTICE)
        except Exception as e:
            RNS.log(f"[wardrive] config error: {e}", RNS.LOG_ERROR)

        try:
            sb = self.get_sideband()
            self._beacon_dest = RNS.Destination(
                sb.identity,
                RNS.Destination.IN,
                RNS.Destination.SINGLE,
                "wardrive", "beacon",
            )
            RNS.log(f"[wardrive] beacon: {RNS.prettyhexrep(self._beacon_dest.hash)}", RNS.LOG_NOTICE)
        except Exception as e:
            RNS.log(f"[wardrive] failed to create beacon destination: {e}", RNS.LOG_ERROR)

        if self._send_mode() == "packet":
            for h in self._bot_hashes():
                try:
                    RNS.Transport.request_path(bytes.fromhex(h))
                    RNS.log(f"[wardrive] path request sent for {h[:8]}…", RNS.LOG_VERBOSE)
                except Exception as e:
                    RNS.log(f"[wardrive] path request failed for {h[:8]}…: {e}", RNS.LOG_ERROR)

        self.should_run = True
        self._thread = threading.Thread(target=self.service_jobs, daemon=True)
        self._thread.start()
        super().start()
        RNS.log("[wardrive] service started", RNS.LOG_NOTICE)

    def stop(self):
        self.should_run = False
        self._trigger.set()
        if hasattr(self, "_thread") and self._thread:
            self._thread.join(timeout=5)
        RNS.log("[wardrive] service stopped", RNS.LOG_NOTICE)
        super().stop()

    def _interval(self):
        try:
            return max(MIN_ANNOUNCE_INTERVAL, int(self._config.get("announce_interval_secs", 30)))
        except (TypeError, ValueError):
            return 30

    def _mode(self):
        return self._config.get("announce_mode", "auto")

    def _send_mode(self):
        return self._config.get("send_mode", "announce")

    def _bot_hashes(self):
        raw = self._config.get("bot_hashes", [])
        if isinstance(raw, str):
            raw = [raw]
        cleaned = []
        for h in raw:
            h = h.replace("<", "").replace(">", "").replace(" ", "")
            if h:
                cleaned.append(h)
        return cleaned

    def _install_announce_hook(self):
        try:
            sb = self.get_sideband()
            if not callable(getattr(sb, "lxmf_announce", None)):
                RNS.log("[wardrive] lxmf_announce not found", RNS.LOG_ERROR)
                return False
            original = sb.lxmf_announce
            plugin   = self
            def hooked(*args, **kwargs):
                result = original(*args, **kwargs)
                RNS.log("[wardrive] announce detected — firing beacon", RNS.LOG_VERBOSE)
                plugin._trigger.set()
                return result
            sb.lxmf_announce = hooked
            RNS.log("[wardrive] announce hook installed", RNS.LOG_NOTICE)
            return True
        except Exception as e:
            RNS.log(f"[wardrive] hook install failed: {e}", RNS.LOG_ERROR)
            return False

    def service_jobs(self):
        RNS.log(f"[wardrive] service_jobs running, mode={self._mode()}", RNS.LOG_DEBUG)
        while self.should_run:
            try:
                if self._mode() == "manual":
                    if not self._patched:
                        self._patched = self._install_announce_hook()
                    self._trigger.wait(timeout=1.0)
                    if self._trigger.is_set() and self.should_run:
                        self._trigger.clear()
                        self._send()
                else:
                    now = time.time()
                    if now - self._last_send >= self._interval():
                        self._send()
                        self._last_send = now
                    time.sleep(1)
            except Exception as e:
                RNS.log(f"[wardrive] error: {e}", RNS.LOG_ERROR)

    def _distance_m(self, lat1, lon1, lat2, lon2):
        dlat = (lat2 - lat1) * 111320
        dlon = (lon2 - lon1) * 111320 * math.cos(math.radians(lat1))
        return math.sqrt(dlat ** 2 + dlon ** 2)

    def _get_location(self):
        sb = self.get_sideband()
        location = None
        if sb.telemeter is not None:
            sb.telemeter.enable("location")
            location = sb.telemeter.read("location")
        if location is None and sb.latest_telemetry is not None:
            location = sb.latest_telemetry.get("location")
        return location

    def _send(self):
        if self._beacon_dest is None:
            return

        location = self._get_location()
        if location is None:
            RNS.log("[wardrive] no location (enable telemetry+location in Sideband settings)", RNS.LOG_DEBUG)
            return

        lat = float(location["latitude"])
        lon = float(location["longitude"])

        if self._mode() == "auto" and self._last_lat is not None:
            dist = self._distance_m(self._last_lat, self._last_lon, lat, lon)
            if dist < MIN_ANNOUNCE_DISTANCE:
                RNS.log(f"[wardrive] skipped, only moved {dist:.1f}m", RNS.LOG_DEBUG)
                return

        if self._send_mode() == "packet":
            self._send_packet(lat, lon)
        else:
            self._send_announce(lat, lon)

        self._last_lat = lat
        self._last_lon = lon

    def _send_announce(self, lat, lon):
        payload = struct.pack(ANNOUNCE_PAYLOAD_FMT, lat, lon)
        self._beacon_dest.announce(app_data=payload)
        RNS.log(f"[wardrive] announced ({lat:.5f}, {lon:.5f})", RNS.LOG_VERBOSE)

    def _send_packet(self, lat, lon):
        hashes = self._bot_hashes()
        if not hashes:
            RNS.log("[wardrive] bot_hashes not set in config, can't send packet", RNS.LOG_ERROR)
            return

        payload = struct.pack(PACKET_PAYLOAD_FMT, lat, lon, bytes(self._beacon_dest.hash))
        for bot_hash_hex in hashes:
            try:
                bot_hash = bytes.fromhex(bot_hash_hex)
            except ValueError:
                RNS.log(f"[wardrive] invalid bot_hash: {bot_hash_hex}", RNS.LOG_ERROR)
                continue

            identity = RNS.Identity.recall(bot_hash)
            if identity is None:
                RNS.Transport.request_path(bot_hash)
                RNS.log(f"[wardrive] {bot_hash_hex[:8]}… identity unknown, path requested — will retry next cycle", RNS.LOG_NOTICE)
                continue

            dest   = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "wardrive", "collector")
            packet = RNS.Packet(dest, payload)
            packet.send()
            RNS.log(f"[wardrive] packet sent to {bot_hash_hex[:8]}… ({lat:.5f}, {lon:.5f})", RNS.LOG_VERBOSE)


plugin_class = WardrivPlugin
