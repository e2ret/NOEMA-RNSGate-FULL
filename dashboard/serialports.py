"""
NOEMA serial ports: which USB serial devices (RNode, other radios) the gateway sees right now.

    GET /api/serial/ports   {"ports": [{"path", "stable", "name", "usb_id", "in_config"}]}
        path       /dev/ttyACM0 or /dev/ttyUSB0
        stable     the same device under /dev/serial/by-id/... (does not change after a reboot), or null
        name       what the device calls itself (manufacturer + product), if it tells
        usb_id     vendor:product, for example 303a:1001
        in_config  name of the Reticulum interface that already uses this port, or null

View only: nothing is opened, written or switched.
"""

import glob
import os
import re


def _usb_info(tty):
    """(usb_id, name) of the USB device behind /dev/<tty>, from sysfs; ('', '') when unknown."""
    try:
        d = os.path.realpath("/sys/class/tty/%s/device" % tty)
        for _ in range(5):                       # the USB device is a few levels above the tty
            if os.path.isfile(os.path.join(d, "idVendor")):
                def rd(n):
                    try:
                        return open(os.path.join(d, n)).read().strip()
                    except OSError:
                        return ""
                name = " ".join(x for x in (rd("manufacturer"), rd("product")) if x)
                return "%s:%s" % (rd("idVendor"), rd("idProduct")), name
            d = os.path.dirname(d)
    except OSError:
        pass
    return "", ""


def configured_ports(config_text):
    """{port: interface name} for the interfaces of the Reticulum config that have a `port = /dev/...` line."""
    out, cur = {}, None
    for line in config_text.split("\n"):
        h = re.match(r"^\s*\[\[(.+?)\]\]\s*$", line)
        if h:
            cur = h.group(1).strip()
            continue
        if re.match(r"^\s*\[", line):
            cur = None
            continue
        m = re.match(r"^\s*port\s*=\s*(/dev/\S+)", line)
        if cur and m:
            out[m.group(1)] = cur
    return out


def list_ports(config_text="", dev="/dev", by_id=None):
    by_id = by_id if by_id is not None else _by_id()
    used = configured_ports(config_text)
    ports = []
    for p in sorted(glob.glob(dev + "/ttyACM*") + glob.glob(dev + "/ttyUSB*")):
        usb_id, name = _usb_info(os.path.basename(p))
        stable = next((link for link, target in by_id.items() if target == p), None)
        ports.append({"path": p, "stable": stable, "name": name, "usb_id": usb_id,
                      "in_config": used.get(p) or (used.get(stable) if stable else None)})
    return ports


def _by_id():
    out = {}
    for link in glob.glob("/dev/serial/by-id/*"):
        try:
            out[link] = os.path.realpath(link)
        except OSError:
            pass
    return out


def register_routes(app, home, prefix="/api/serial"):
    from flask import jsonify

    @app.route(prefix + "/ports")
    def serial_ports():
        try:
            text = open(os.path.join(home, ".reticulum", "config"), encoding="utf-8").read()
        except OSError:
            text = ""
        return jsonify({"ports": list_ports(text)})
