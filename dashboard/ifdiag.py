"""
NOEMA interface diagnostics: problems of the Reticulum interfaces in plain words.

Three kinds of findings, all from data the stack already has (nothing is changed or switched off):
    down      an enabled interface is offline (from the stack's interface statistics)
    reset     a network interface keeps losing its connection (from the stack's own warnings, last 30 min)
    restarts  the dashboard was started many times in 12 hours (some public hubs may then refuse the address)

Route (registered in app.py):
    GET /api/diag/interfaces   {"issues": [{"kind", "level", "iface", "count", "detail"}]}
"""

import json
import os
import re
import threading
import time
from collections import deque

EVENT_RE = re.compile(r"interface error occurred for (\S+?\[[^\]]*\]).*?contained exception was:\s*(.*)$", re.S)
WINDOW = 30 * 60                 # seconds a connection problem counts
RESET_MIN = 3                    # this many in the window makes a finding
RESET_BAD = 10                   # ... and this many makes it an error
RESTART_WINDOW = 12 * 3600
RESTART_MIN = 5


def _reason(text):
    t = text.lower()
    if "reset by peer" in t or "errno 104" in t:
        return "reset"
    if "eof" in t or "closed" in t:
        return "closed"
    if "refused" in t or "errno 111" in t:
        return "refused"
    if "timed out" in t or "timeout" in t:
        return "timeout"
    return "other"


class Diag:
    def __init__(self, base_dir):
        self.events = deque(maxlen=400)          # (time, interface, reason)
        self.lock = threading.Lock()
        self.path = os.path.join(base_dir, "restarts.json")
        self.restarts = self._note_start()

    def _note_start(self):
        now = time.time()
        try:
            with open(self.path) as f:
                lst = [float(x) for x in json.load(f)]
        except Exception:
            lst = []
        lst = [x for x in lst if now - x < 7 * 86400][-60:] + [now]
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(lst, f)
            os.replace(tmp, self.path)
        except Exception:
            pass
        return lst

    def capture(self, message, now=None):
        """Remember one stack warning about an interface error (other messages are ignored)."""
        m = EVENT_RE.search(str(message))
        if m:
            with self.lock:
                self.events.append((time.time() if now is None else now, m.group(1), _reason(m.group(2))))

    def issues(self, stats, now=None):
        now = time.time() if now is None else now
        out = []
        ifaces = []
        if isinstance(stats, dict):
            ifaces = [i for i in (stats.get("interfaces") or []) if isinstance(i, dict)]
        # 1. interfaces that are offline
        for i in ifaces:
            name = str(i.get("name") or i.get("short_name") or "")
            if not name or "LocalInterface" in name or "Shared Instance" in name:
                continue
            if i.get("status") is False:
                out.append({"kind": "down", "level": "error", "iface": name, "count": 1, "detail": ""})
        # 2. network interfaces that keep losing the connection
        with self.lock:
            recent = [e for e in self.events if now - e[0] <= WINDOW]
        by = {}
        for t, name, reason in recent:
            by.setdefault(name, []).append(reason)
        for name, reasons in by.items():
            if len(reasons) >= RESET_MIN:
                last = max(set(reasons), key=reasons.count)
                out.append({"kind": "reset", "level": "error" if len(reasons) >= RESET_BAD else "warn",
                            "iface": name, "count": len(reasons), "detail": last})
        # 3. too many starts of the dashboard while network interfaces exist
        starts = [x for x in self.restarts if now - x <= RESTART_WINDOW]
        has_net = any(("TCP" in str(i.get("type") or i.get("name") or "") or "Backbone" in str(i.get("type") or i.get("name") or ""))
                      for i in ifaces)
        if len(starts) >= RESTART_MIN and has_net:
            out.append({"kind": "restarts", "level": "warn", "iface": "", "count": len(starts), "detail": ""})
        return out
