"""
NOEMA discovered interfaces: network interfaces that other Reticulum nodes announce (RNS 1.6+).

The stack listens for these announcements when `discover_interfaces = yes` is set in the [reticulum]
section and stores each one as a small file in <config>/storage/discovery/interfaces. This module only
reads those files and, on request, writes one config entry into the Reticulum config (backup first).
Nothing is connected by itself and nothing is announced.

Routes (registered with register_routes):
    GET  /api/discovered          {"enabled", "available", "items": [...]}
    POST /api/discovered/enable   {"confirm": true}   set discover_interfaces = yes (backup first)
    POST /api/discovered/add      {"id": <discovery hash>, "confirm": true}   add the interface to the config
Both writes need a restart of the dashboard to take effect.
"""

import json
import math
import os
import re
import shutil
import time

STALE = 3 * 86400
UNKNOWN = 86400
ADDABLE = ("BackboneInterface", "TCPServerInterface", "I2PInterface")
HOST_OK = re.compile(r"^[A-Za-z0-9._:\-]{1,253}$")
HEX_OK = re.compile(r"^[0-9a-fA-F]{1,128}$")
TEXT_OK = re.compile(r"^[A-Za-z0-9 ._\-]{1,60}$")


def _unpack(data):
    try:
        from RNS.vendor import umsgpack
        return umsgpack.unpackb(data)
    except Exception:
        import msgpack
        return msgpack.unpackb(data, raw=False)


def _s(v):
    if isinstance(v, bytes):
        v = v.decode("utf-8", "ignore")
    return v


def _hex(v):
    return v.hex() if isinstance(v, (bytes, bytearray)) else str(v or "")


def _km(a, b):
    """Great-circle distance in km between (lat, lon) pairs, None if a value is missing."""
    try:
        la1, lo1, la2, lo2 = [math.radians(float(x)) for x in (a[0], a[1], b[0], b[1])]
    except (TypeError, ValueError):
        return None
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _status(age):
    return "stale" if age > STALE else "unknown" if age > UNKNOWN else "available"


def read_all(folder, now=None, origin=None):
    """[{id, name, type, status, ...}] from the discovery files, best first; unreadable files are skipped."""
    now = time.time() if now is None else now
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for fn in names:
        try:
            with open(os.path.join(folder, fn), "rb") as f:
                d = _unpack(f.read())
            d = {_s(k): _s(v) for k, v in d.items()}
            age = max(0, now - float(d.get("last_heard") or 0))
            item = {
                "id": fn, "name": str(d.get("name") or ""), "type": str(d.get("type") or ""),
                "status": _status(age), "age": int(age), "hops": d.get("hops"), "value": d.get("value"),
                "transport": bool(d.get("transport")), "heard": d.get("heard_count"),
                "host": str(d.get("reachable_on") or ""), "port": d.get("port"),
                "frequency": d.get("frequency"), "bandwidth": d.get("bandwidth"), "sf": d.get("sf"), "cr": d.get("cr"),
                "lat": d.get("latitude"), "lon": d.get("longitude"),
                "protected": bool(d.get("ifac_netname") or d.get("ifac_netkey")),
                "impl": (str(d.get("impl_name") or "") + " " + str(d.get("version") or "")).strip(),
            }
            km = _km(origin, (item["lat"], item["lon"])) if origin and item["lat"] is not None and item["lon"] is not None else None
            item["km"] = None if km is None else round(km)
            item["addable"] = item["type"] in ADDABLE and _entry(d) is not None
            out.append(item)
        except Exception:
            continue
    rank = {"available": 0, "unknown": 1, "stale": 2}
    out.sort(key=lambda i: (rank[i["status"]], i["hops"] if isinstance(i["hops"], int) else 99, i["age"]))
    return out


def _entry(d, taken=()):
    """Config text for one discovered interface, built here from validated values (never copied verbatim); None if unusable."""
    d = {_s(k): _s(v) for k, v in d.items()}
    typ = str(d.get("type") or "")
    if typ not in ADDABLE:
        return None
    name = re.sub(r"[^A-Za-z0-9 ._\-]", "", str(d.get("name") or "")).strip()[:40] or "discovered"
    base, n = name, 2
    while name in taken:
        name, n = "%s %d" % (base, n), n + 1
    lines = ["  [[%s]]" % name]
    host = str(d.get("reachable_on") or "")
    if not HOST_OK.match(host):
        return None
    if typ == "I2PInterface":
        lines += ["    type = I2PInterface", "    enabled = yes", "    peers = %s.b32.i2p" % host]
    else:
        try:
            port = int(d.get("port"))
        except (TypeError, ValueError):
            return None
        if not 1 <= port <= 65535:
            return None
        lines += ["    type = TCPClientInterface", "    enabled = yes", "    target_host = %s" % host, "    target_port = %d" % port]
    tid = _hex(d.get("transport_id"))
    if tid and HEX_OK.match(tid):
        lines.append("    transport_identity = %s" % tid)
    nn, nk = d.get("ifac_netname"), d.get("ifac_netkey")
    if nn and TEXT_OK.match(str(nn)):
        lines.append("    network_name = %s" % nn)
    if nk and TEXT_OK.match(str(nk)):
        lines.append("    passphrase = %s" % nk)
    return name, "\n".join(lines) + "\n"


def _section_end(lines, header):
    """Index where a new line can be added at the end of the section `header` (before the next top-level section)."""
    start = next((i for i, l in enumerate(lines) if l.strip().lower() == header), None)
    if start is None:
        return None, None
    for i in range(start + 1, len(lines)):
        s = lines[i].strip()
        if s.startswith("[") and not s.startswith("[["):
            return start, i
    return start, len(lines)


def discover_enabled(text):
    m = re.search(r"^\s*discover_interfaces\s*=\s*([^#\n]*)", text, re.I | re.M)
    return bool(m) and m.group(1).strip().lower() in ("yes", "true", "1", "on")


def set_discover(text):
    """Text with `discover_interfaces = yes` in the [reticulum] section."""
    if re.search(r"^\s*discover_interfaces\s*=", text, re.I | re.M):
        return re.sub(r"^(\s*discover_interfaces\s*=\s*)[^#\n]*?(\s*(#.*)?)$", r"\g<1>yes\g<2>", text, count=1, flags=re.I | re.M)
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if l.strip().lower() == "[reticulum]"), None)
    if start is None:
        raise ValueError("no [reticulum] section")
    lines.insert(start + 1, "  discover_interfaces = yes")
    return "\n".join(lines)


def add_entry(text, d):
    """(new_text, name) with the discovered interface added to the [interfaces] section."""
    from profiles import parse
    taken = {s["name"] for s in parse(text)}
    e = _entry(d, taken)
    if e is None:
        raise ValueError("this interface cannot be added")
    name, block = e
    lines = text.split("\n")
    start, end = _section_end(lines, "[interfaces]")
    if start is None:
        lines += ["", "[interfaces]"]
        end = len(lines)
    while end > 0 and not lines[end - 1].strip():      # keep the blank lines after the section where they were
        end -= 1
    lines[end:end] = [""] + block.rstrip("\n").split("\n")
    return "\n".join(lines), name


class Discovered:
    def __init__(self, home):
        self.cfg = os.path.join(home, ".reticulum", "config")
        self.folder = os.path.join(home, ".reticulum", "storage", "discovery", "interfaces")
        self.netmap = os.path.join(home, "dashboard", "netmap.json")

    def _text(self):
        with open(self.cfg) as f:
            return f.read()

    def _write(self, new, tag):
        backup = self.cfg + ".bak-" + tag + "-" + time.strftime("%Y%m%d_%H%M%S")
        shutil.copy2(self.cfg, backup)
        tmp = self.cfg + ".tmp"
        with open(tmp, "w") as f:
            f.write(new)
        os.replace(tmp, self.cfg)
        return backup

    def listing(self):
        text = self._text()
        try:
            with open(self.netmap) as f:
                o = json.load(f).get("origin")
            origin = (o["lat"], o["lon"]) if o else None
        except Exception:
            origin = None
        items = read_all(self.folder, origin=origin)
        from profiles import parse
        have = {(s["name"]) for s in parse(text)}
        for i in items:                                  # already in the config (same name or same host:port)
            i["added"] = i["name"] in have or bool(i["host"] and re.search(re.escape(i["host"]), text) and (not i["port"] or re.search(r"\b%s\b" % re.escape(str(i["port"])), text)))
        return {"enabled": discover_enabled(text), "items": items}

    def enable(self):
        text = self._text()
        if discover_enabled(text):
            return None
        return self._write(set_discover(text), "discover")

    def add(self, ident):
        if not re.match(r"^[0-9a-fA-F]{8,128}$", ident or ""):
            raise ValueError("bad id")
        with open(os.path.join(self.folder, ident), "rb") as f:
            d = _unpack(f.read())
        new, name = add_entry(self._text(), d)
        return name, self._write(new, "discovered")


def register_routes(app, home, prefix="/api/discovered"):
    from flask import jsonify, request
    ds = Discovered(home)

    def guard(fn):
        try:
            return fn()
        except (ValueError, OSError) as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    def body():
        return request.get_json(silent=True) or {}

    @app.route(prefix)
    def discovered_list():
        return guard(lambda: jsonify(ds.listing()))

    @app.route(prefix + "/enable", methods=["POST"])
    def discovered_enable():
        def run():
            if body().get("confirm") is not True:
                raise ValueError("confirmation required")
            return jsonify({"ok": True, "backup": ds.enable()})
        return guard(run)

    @app.route(prefix + "/add", methods=["POST"])
    def discovered_add():
        def run():
            if body().get("confirm") is not True:
                raise ValueError("confirmation required")
            name, backup = ds.add(str(body().get("id") or ""))
            return jsonify({"ok": True, "name": name, "backup": backup})
        return guard(run)

    return ds
