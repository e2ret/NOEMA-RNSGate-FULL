#!/usr/bin/env python3
"""
NOEMA TCP Watchdog — self-healing for TCPClientInterface connections.

RNS does not automatically redial a dropped TCPClientInterface until the
next path request needs it. On flaky internet (power outages, ISP drops),
an interface can sit dead for a long time. This script checks `rnstatus`
periodically and restarts rnsd if any TCP interface is reported Down,
with a cooldown so it doesn't restart-storm during an extended outage.

Run via cron every 5 minutes:
    */5 * * * * /path/to/.venv/bin/python3 /path/to/tcp_watchdog.py
"""

import subprocess
import time
import os
import sys

RNSD_STATUS_CACHE = "/tmp/noema_tcp_watchdog_last_restart"
COOLDOWN_SECONDS = 600  # don't restart more than once per 10 minutes


def get_rnstatus_json():
    try:
        result = subprocess.run(
            ["rnstatus", "-j"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return None
        import json
        return json.loads(result.stdout)
    except Exception as e:
        print(f"[watchdog] rnstatus failed: {e}")
        return None


def has_down_tcp_interface(status):
    if not status:
        return False
    interfaces = status.get("interfaces", status) if isinstance(status, dict) else status
    if isinstance(interfaces, dict):
        interfaces = interfaces.values()
    for iface in interfaces or []:
        if not isinstance(iface, dict):
            continue
        itype = str(iface.get("type", "")).lower()
        if "tcpclient" not in itype:
            continue
        if not iface.get("status", True) and not iface.get("up", True):
            return True
        # Some rnstatus versions use a status string
        status_str = str(iface.get("status", "")).lower()
        if status_str in ("down", "false", "0"):
            return True
    return False


def last_restart_ok(cooldown):
    if not os.path.exists(RNSD_STATUS_CACHE):
        return True
    try:
        with open(RNSD_STATUS_CACHE) as f:
            last = float(f.read().strip())
        return (time.time() - last) > cooldown
    except Exception:
        return True


def mark_restarted():
    with open(RNSD_STATUS_CACHE, "w") as f:
        f.write(str(time.time()))


def main():
    status = get_rnstatus_json()
    if status is None:
        print("[watchdog] Could not read rnstatus, skipping this cycle")
        return

    if not has_down_tcp_interface(status):
        return  # all good

    if not last_restart_ok(COOLDOWN_SECONDS):
        print("[watchdog] Down interface detected but still in cooldown, skipping")
        return

    print("[watchdog] Down TCP interface detected — restarting rnsd")
    try:
        subprocess.run(["systemctl", "restart", "rnsd"], timeout=30)
        mark_restarted()
    except Exception as e:
        print(f"[watchdog] Restart failed: {e}")


if __name__ == "__main__":
    main()
