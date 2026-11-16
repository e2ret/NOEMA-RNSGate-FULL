"""Parsing and validation of HaLow RNode telemetry received over MQTT.

Pure functions, no I/O: map_server.py feeds each message from the broker through
parse_telemetry() and keeps what comes back.

The nodes publish JSON to one topic (rnode-halow/telemetry) using a username and
password that are public knowledge, so anyone can publish there. Everything here is
therefore treated as hostile input. In particular the page builds its popups with raw
innerHTML from the name / fw / freq / bw fields, so those are reduced to a character
allow-list rather than passed through.

Example payload:
  {"id":3328097428,"name":"Catz Node (Halow)","fw":"2.3.0","loc":"55.643070,37.527630",
   "freq":"864.5 MHz","bw":"1 MHz","uptime":536345,"mcs":0,"lxmf":"aa72...e3ce17e52e",
   "tx_b":53952752,"rx_b":0,"tx_p":409716,"rx_p":0}
"""
import json
import math
import re

MAX_PAYLOAD = 4096

_NAME_BAD = re.compile(r"[^\w .,:+()/-]")      # \w is Unicode-aware, so Cyrillic names survive
_SHORT_OK = re.compile(r"[\w .,+/-]{1,32}")
_LXMF_OK = re.compile(r"[0-9a-f]{32}")
_ZERO_LXMF = "0" * 32


def _count(v, hi=2**53):
    """A non-negative integer counter, or None. bool is an int subclass, so exclude it."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and not v.is_integer():
        return None
    v = int(v)
    return v if 0 <= v <= hi else None


def _short(v):
    return v if isinstance(v, str) and _SHORT_OK.fullmatch(v) else ""


def _position(loc):
    """'lat,lon' -> (lat, lon), or None when absent, malformed or the unset 0,0."""
    if not isinstance(loc, str):
        return None
    parts = loc.split(",")
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if abs(lat) > 90 or abs(lon) > 180 or (lat == 0 and lon == 0):
        return None
    return round(lat, 6), round(lon, 6)


def node_key(lxmf, node_id, name):
    """Identity of a physical node across messages.

    The LXMF address is preferred. A node with no address configured reports all
    zeros, which every such node shares, so those fall back to the chip id.
    """
    if isinstance(lxmf, str) and _LXMF_OK.fullmatch(lxmf) and lxmf != _ZERO_LXMF:
        return lxmf
    if _count(node_id) is not None:
        return f"id:{int(node_id)}"
    return f"name:{name}"


def parse_telemetry(payload, now):
    """One MQTT payload -> (key, node dict) in the shape /api/discovered serves, or None."""
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > MAX_PAYLOAD:
        return None
    try:
        d = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(d, dict):
        return None
    pos = _position(d.get("loc"))
    if pos is None:
        return None

    raw_name = d.get("name")
    name = _NAME_BAD.sub("", raw_name).strip()[:64] if isinstance(raw_name, str) else ""
    if not name:
        name = "HaLow node"
    lxmf = d.get("lxmf") if isinstance(d.get("lxmf"), str) else ""
    lxmf = lxmf if _LXMF_OK.fullmatch(lxmf) else ""

    node = {
        "name":       name,
        "type":       "HaLow",
        "type_raw":   "HaLowInterface",
        "lat":        pos[0],
        "lon":        pos[1],
        "last_heard": int(now),
        "freq_str":   _short(d.get("freq")),
        "bw_str":     _short(d.get("bw")),
        "fw":         _short(d.get("fw")),
        "uptime":     _count(d.get("uptime")),
        "lxmf":       lxmf,
        "mcs":        _count(d.get("mcs"), 31),
    }
    tx, rx = _count(d.get("tx_p")), _count(d.get("rx_p"))
    if tx is not None:
        node["tx_p"] = tx
        node["rx_p"] = rx or 0
    return node_key(lxmf, d.get("id"), name), node
