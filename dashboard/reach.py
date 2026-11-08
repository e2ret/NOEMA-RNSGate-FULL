"""
NOEMA reachability checks for one destination: ask the network for a path, and send a probe.

Both work only when you press the button (nothing is sent in the background) and change nothing.

    POST /api/reach/path    {"hash": <hex>, "wait": seconds <= 20}
        {"ok", "known", "hops", "interface", "waited", "requested"}
        Uses the path the stack already knows; if there is none, asks the network (like `rnpath -r`) and waits.
    POST /api/reach/probe   {"hash": <hex>, "timeout": seconds <= 30}
        {"ok", "delivered", "rtt_ms", "hops", "rssi", "snr", "error"}
        Sends one probe packet to the node's probe address (like `rnprobe rnstransport.probe`). Only Reticulum
        transport nodes that answer probes reply; an ordinary chat or Nomad node usually stays silent.

`iface_of` is a function hash(bytes) -> interface text or None, given by app.py (the interface is found through
the instance that owns it when the dashboard is only a client of it).
"""

import os
import re
import time

HEX = re.compile(r"^[0-9a-fA-F]{32}$")


def _num(v, default, lo, hi):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def check_path(RNS, h, wait, iface_of=None, sleep=time.sleep):
    """Path to destination h (bytes): known hops and interface, asking the network first when none is known."""
    requested = False
    t0 = time.time()
    if not RNS.Transport.has_path(h):
        requested = True
        RNS.Transport.request_path(h)
        while not RNS.Transport.has_path(h) and time.time() - t0 < wait:
            sleep(0.2)
    known = bool(RNS.Transport.has_path(h))
    out = {"ok": True, "known": known, "requested": requested, "waited": round(time.time() - t0, 1),
           "hops": None, "interface": None}
    if known:
        out["hops"] = RNS.Transport.hops_to(h)
        try:
            out["interface"] = iface_of(h) if iface_of else None
        except Exception:
            pass
    return out


def probe(RNS, h, timeout, sleep=time.sleep):
    """One probe to the probe address of the identity behind destination h (bytes)."""
    identity = RNS.Identity.recall(h)
    if identity is None:                                   # the identity is learnt from an announce or a path answer
        RNS.Transport.request_path(h)
        t0 = time.time()
        while identity is None and time.time() - t0 < min(timeout, 8):
            sleep(0.3)
            identity = RNS.Identity.recall(h)
    if identity is None:
        return {"ok": True, "delivered": False, "error": "unknown"}
    dest = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "rnstransport", "probe")
    ph = dest.hash
    if not RNS.Transport.has_path(ph):
        RNS.Transport.request_path(ph)
        t0 = time.time()
        while not RNS.Transport.has_path(ph) and time.time() - t0 < timeout:
            sleep(0.2)
    if not RNS.Transport.has_path(ph):
        return {"ok": True, "delivered": False, "error": "no_path"}
    packet = RNS.Packet(dest, os.urandom(16))
    receipt = packet.send()
    if receipt is None:
        return {"ok": True, "delivered": False, "error": "send"}
    t0 = time.time()
    while receipt.status == RNS.PacketReceipt.SENT and time.time() - t0 < timeout:
        sleep(0.1)
    if receipt.status != RNS.PacketReceipt.DELIVERED:
        return {"ok": True, "delivered": False, "error": "silent"}
    out = {"ok": True, "delivered": True, "rtt_ms": round(receipt.get_rtt() * 1000, 1), "hops": RNS.Transport.hops_to(ph),
           "rssi": None, "snr": None}
    try:
        pp = receipt.proof_packet
        inst = RNS.Reticulum.get_instance()
        if inst.is_connected_to_shared_instance:
            out["rssi"], out["snr"] = inst.get_packet_rssi(pp.packet_hash), inst.get_packet_snr(pp.packet_hash)
        else:
            out["rssi"], out["snr"] = pp.rssi, pp.snr
    except Exception:
        pass
    return out


def register_routes(app, RNS, iface_of=None, prefix="/api/reach"):
    from flask import jsonify, request

    def get_hash():
        h = str((request.get_json(silent=True) or {}).get("hash") or "").strip().lower()
        if not HEX.match(h):
            raise ValueError("bad hash")
        return bytes.fromhex(h)

    def guard(fn):
        try:
            return fn()
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except Exception as e:
            return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500

    @app.route(prefix + "/path", methods=["POST"])
    def reach_path():
        return guard(lambda: jsonify(check_path(RNS, get_hash(), _num((request.get_json(silent=True) or {}).get("wait"), 10, 1, 20), iface_of)))

    @app.route(prefix + "/probe", methods=["POST"])
    def reach_probe():
        return guard(lambda: jsonify(probe(RNS, get_hash(), _num((request.get_json(silent=True) or {}).get("timeout"), 15, 3, 30))))
