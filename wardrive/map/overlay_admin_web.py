#!/opt/reticulum/bin/python3
"""
overlay_admin_web.py - web admin for the community overlay, for the operator.

Shows contributors (who invited whom, usage against quota), invites, shared sessions and why
any were rejected, and a plot of where a session's entries are. It can also act on them: mint
an invite, add a contributor, rename, change limits, rotate a token, ban (optionally with
everyone they invited), reinstate, remove a session, void an invite. Every change is
recorded in the overlay database (admin_log, shown as Activity) and in the service log.

Reading and writing are kept apart. Pages are built from a read-only connection (mode=ro,
query_only). Changes are POSTs that go through OverlayStore, the same code the server and
overlay_admin.py use. A POST must carry a CSRF token tied to the signed-in user, come from
this same origin, and be a small urlencoded form; ban, token rotation and session removal
also only run from their own confirmation page, which says what will be deleted. There is
no JavaScript. Start with --read-only (or "read_only": true under "overlay_admin" in the
config) to get a view that refuses every change. overlay_admin.py remains for the shell.

It is a separate process from map_server.py and always binds to 127.0.0.1; there is no
option to listen anywhere else. Two ways to reach it:

  On demand, through an SSH tunnel -- SSH is the only credential:

    ssh -L 8850:127.0.0.1:8850 srv1 /opt/reticulum/bin/overlay_admin_web.py
    # then open http://127.0.0.1:8850/

  Published on the map's nginx site behind a password (map/nginx/wardrive-map-admin.conf):

    overlay_admin_web.py --base-path /overlay-admin --public-host map.rns.moscow
    # or set them once in wardrive_map.json: "overlay_admin": {"base_path": ..., "public_hosts": [...]}

  nginx does the password check (auth_basic) and forwards the signed-in name in
  X-Remote-User. Giving --public-host switches on a second, fail-closed check here:
  a request that arrives without that header is refused, so a vhost that loses its
  auth_basic line serves an error instead of the data. Without --public-host the
  header is not required (the SSH-tunnel case has no nginx in front).

Stdlib only. Everything that came from a contributor is HTML-escaped.
"""

import argparse
import hashlib
import hmac
import html
import json
import math
import os
import re
import sqlite3
import secrets
import statistics
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from overlay_store import MAX_LIMIT, OverlayStore, StoreError

CONFIG_PATH = "/etc/reticulum/wardrive_map.json"
PLOT_MAX_POINTS = 5000
MAX_FORM_BYTES = 8192

CSS = """
:root { --bg:#0a0e0a; --card:#0e1a0e; --line:#1b3520; --text:#b8d4b8; --dim:#7f9f7f;
        --accent:#00cc44; --bad:#e57373; --warn:#e0a030; color-scheme: dark; }
@media (prefers-color-scheme: light) {
  :root { --bg:#fff; --card:#f4f4f4; --line:#ddd; --text:#222; --dim:#666;
          --accent:#2a8a3a; --bad:#c62828; --warn:#a06800; color-scheme: light; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:13px/1.5 ui-monospace,Menlo,Consolas,monospace; }
main { padding: 14px 16px 40px; }   /* full width: the contributors table has 11 columns */
h1 { font-size: 15px; margin: 4px 0 2px; letter-spacing:.06em; text-transform:uppercase; }
h2 { font-size: 12px; margin: 22px 0 6px; color:var(--dim); letter-spacing:.08em; text-transform:uppercase; }
a { color: var(--accent); text-decoration: none; } a:hover { text-decoration: underline; }
.sub { color: var(--dim); margin: 0 0 8px; }
.cards { display:flex; gap:8px; flex-wrap:wrap; margin: 10px 0; }
.card { background:var(--card); border:1px solid var(--line); border-radius:6px; padding:8px 12px; min-width:120px; }
.card b { display:block; font-size:18px; } .card span { color:var(--dim); font-size:11px; }
.wrap { overflow-x:auto; border:1px solid var(--line); border-radius:6px; }
table { border-collapse: collapse; width:100%; }
th, td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
th { color:var(--dim); font-weight:normal; background:var(--card); }
tr:last-child td { border-bottom:0; }
td.num, th.num { text-align:right; }
table.kv td { white-space: normal; overflow-wrap: anywhere; }
td.wrapcell { white-space: normal; overflow-wrap: anywhere; max-width: 22ch; }   /* long names wrap instead of widening the table */
.wrap.narrow { max-width: 1000px; }
.bad { color:var(--bad); } .warn { color:var(--warn); } .ok { color:var(--accent); } .dim { color:var(--dim); }
.bar { display:inline-block; width:44px; height:6px; background:var(--line); border-radius:3px; vertical-align:middle; margin-left:6px; }
.bar i { display:block; height:100%; background:var(--accent); border-radius:3px; }
.bar.full i { background:var(--bad); }
pre { background:var(--card); border:1px solid var(--line); border-radius:6px; padding:8px 12px; overflow-x:auto; margin:0; }
svg { width:100%; max-width:1000px; height:auto; background:var(--card); border:1px solid var(--line); border-radius:6px; }
.legend { color:var(--dim); margin:6px 0 0; }
.legend i { display:inline-block; width:10px; height:10px; border-radius:50%; vertical-align:middle; margin:0 3px 0 10px; }
footer { color:var(--dim); margin-top:26px; }
.panel { display:flex; flex-wrap:wrap; gap:10px 18px; align-items:flex-end; margin:6px 0 10px; }
form.act { display:inline-flex; flex-wrap:wrap; gap:6px; align-items:center; margin:0; }
form.act label { color:var(--dim); font-size:11px; }
input[type=text], input[type=number] { background:var(--card); color:var(--text); border:1px solid var(--line);
  border-radius:4px; padding:4px 6px; font:inherit; }
input[type=number] { width:9em; } input[name=name] { width:16em; }
.btn, button { background:var(--card); color:var(--text); border:1px solid var(--line); border-radius:4px;
  padding:4px 10px; font:inherit; cursor:pointer; display:inline-block; }
.btn:hover, button:hover { border-color:var(--accent); text-decoration:none; }
.btn.danger, button.danger { color:var(--bad); border-color:var(--bad); }
.flash { border:1px solid var(--accent); border-radius:6px; padding:6px 12px; margin:8px 0; }
.secret { font-size:15px; word-break:break-all; white-space:pre-wrap; user-select:all; }
td form.act button { padding:1px 8px; }
"""


def esc(v):
    return html.escape(str(v), quote=True)


def when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def ago(ts):
    if not ts:
        return "-"
    d = max(0, int(time.time()) - int(ts))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if d >= size:
            return f"{d // size}{unit} ago"
    return f"{d}s ago"


def page(title, body, crumbs="", base="", user="", readonly=False):
    who = f' Signed in as <b>{esc(user)}</b>.' if user else ""
    mode = ' <span class="dim">· read-only</span>' if readonly else ""
    foot = ("Changes are made with <code>overlay_admin.py</code> (remove-batch, ban, limits, invite)."
            if readonly else "Every change made here is recorded in the activity log.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>{esc(title)}</title>
<style>{CSS}</style></head><body><main>
<h1>Community overlay{mode}</h1>
<p class="sub">{crumbs or f'<a href="{esc(base)}/">overview</a>'}</p>
{body}
<footer>{foot} Generated {esc(when(time.time()))}.{who}</footer>
</main></body></html>"""


def table(head, rows, num=(), wrap=()):
    """head: column titles; rows: lists of already-safe HTML cells; num: right-aligned columns;
    wrap: columns whose text may wrap (names), so one long value cannot widen the whole table."""
    if not rows:
        return '<p class="dim">(none)</p>'
    cls = lambda i: ("num " if i in num else "") + ("wrapcell" if i in wrap else "")
    th = "".join(f'<th class="{cls(i)}">{h}</th>' for i, h in enumerate(head))
    tr = "".join("<tr>" + "".join(f'<td class="{cls(i)}">{c}</td>'
                                  for i, c in enumerate(r)) + "</tr>" for r in rows)
    kv = ' class="kv"' if list(head) == ["field", "value"] else ""   # long values wrap instead of scrolling
    return f'<div class="wrap{" narrow" if kv else ""}"><table{kv}><tr>{th}</tr>{tr}</table></div>'


def meter(used, limit):
    pct = 0 if not limit else min(100, round(100 * used / limit))
    return (f'{used}/{limit}<span class="bar{" full" if pct >= 100 else ""}">'
            f'<i style="width:{pct}%"></i></span>')


def detail_summary(raw):
    """A batch's stored detail JSON -> short safe HTML."""
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        return esc(raw)
    bits = []
    if d.get("error"):
        bits.append(f'<span class="bad">{esc(d["error"])}</span>')
    rej = d.get("rejected") or {}
    if rej:
        bits.append(", ".join(f'{esc(k)} ×{esc(v)}' for k, v in sorted(rej.items(), key=lambda kv: -kv[1])))
    if d.get("duplicates"):
        bits.append(f'<span class="dim">{esc(d["duplicates"])} duplicate</span>')
    return " · ".join(bits) or '<span class="dim">clean</span>'


STATUS_CLASS = {"active": "ok", "published": "ok", "banned": "bad", "rejected": "warn", "removed": "dim"}
# The app marks a session it sent "Shared"; the database keeps calling that state "published".
STATUS_LABEL = {"published": "shared"}


def status(s):
    return f'<span class="{STATUS_CLASS.get(s, "")}">{esc(STATUS_LABEL.get(s, s))}</span>'


# One line in the sessions tables is one file the app (or the map page) sent, which is normally one session.
SESSION_NOTE = ('<p class="dim">One line per shared file: normally one session. A very long session is split into '
                "several files, and a file exported from the map page can hold several sessions.</p>")

# Fixed texts for the redirect-after-POST banner: the URL carries a code, never free text.
FLASH = {
    "renamed":    "Renamed.",
    "limits":     "Limits saved.",
    "reinstated": "Reinstated. What the ban deleted is not restored.",
    "banned":     "Banned. {n} entries deleted.",
    "removed":    "Session removed. {n} entries taken off the map.",
    "voided":     "Invite voided.",
}

# What a refused action says, by the store's error code.
ERRORS = {
    "bad_name":              "That name is not valid.",
    "bad_limit":             f"Limits must be whole numbers from 0 to {MAX_LIMIT:,}.",
    "no_such_contributor":   "No such contributor.",
    "no_such_batch":         "No such session.",
    "no_such_invite":        "No such invite.",
    "invite_used":           "That invite has already been used.",
    "banned":                "That contributor is banned; reinstate them first.",
    "not_banned":            "That contributor is not banned.",
}


class View:
    """Every page, built from read-only queries. Changes never go through here: see Handler."""

    def __init__(self, db_path, base="", csrf_secret=b"", can_write=False):
        self.base = base            # URL prefix when served below a path, e.g. /overlay-admin
        self.uri = "file:" + db_path.replace("?", "%3f").replace("#", "%23") + "?mode=ro"
        self.can_write = can_write  # the forms below are only drawn when the server can act on them
        self._csrf_secret = csrf_secret

    # -- forms: every change is a POST carrying a token tied to the signed-in user --

    def csrf(self, user):
        return hmac.new(self._csrf_secret, (user or "").encode("utf-8"), hashlib.sha256).hexdigest()

    def csrf_ok(self, user, token):
        return bool(self._csrf_secret) and hmac.compare_digest(self.csrf(user), str(token or ""))

    def pg(self, title, body, crumbs="", user=""):
        return page(title, body, crumbs, self.base, user, readonly=not self.can_write)

    def form(self, action, user, button, fields="", cls="", extra=None, confirm=False):
        hidden = f'<input type="hidden" name="csrf" value="{self.csrf(user)}">'
        for k, v in (extra or {}).items():
            hidden += f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">'
        if confirm:
            hidden += '<input type="hidden" name="confirm" value="1">'
        return (f'<form method="post" action="{esc(self.base)}/do/{esc(action)}" class="act">{hidden}{fields}'
                f'<button type="submit" class="{cls}">{esc(button)}</button></form>')

    def link_button(self, path, text, cls=""):
        return f'<a class="btn {cls}" href="{esc(self.base)}{esc(path)}">{esc(text)}</a>'

    @staticmethod
    def flash_html(msg, n=None):
        text = FLASH.get(msg)
        return f'<p class="flash">{esc(text.format(n=n if n is not None else 0))}</p>' if text else ""

    def q(self, sql, args=()):
        c = sqlite3.connect(self.uri, uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA query_only=ON")
            return c.execute(sql, args).fetchall()
        finally:
            c.close()

    def contributor_link(self, cid, name):
        return f'<a href="{esc(self.base)}/contributor/{int(cid)}">{esc(name)}</a>'

    def overview(self, user="", flash=""):
        since = int(time.time()) - 86400
        tot = self.q("SELECT (SELECT COUNT(*) FROM samples) s, (SELECT COUNT(*) FROM batches WHERE status='published') b,"
                     " (SELECT COUNT(*) FROM contributors WHERE status='active') a,"
                     " (SELECT COUNT(*) FROM contributors WHERE status='banned') x,"
                     " (SELECT COUNT(*) FROM batches WHERE ts>=? AND status='rejected') r", (since,))[0]
        cards = "".join(f'<div class="card"><b>{esc(v)}</b><span>{esc(k)}</span></div>' for k, v in (
            ("entries on the map", tot["s"]), ("shared sessions", tot["b"]), ("active contributors", tot["a"]),
            ("banned", tot["x"]), ("rejected sessions, 24 h", tot["r"])))

        rows = []
        for r in self.q(
                "SELECT c.*, i.name AS inviter,"
                " (SELECT COUNT(*) FROM samples s WHERE s.contributor_id=c.id) AS samples,"
                " (SELECT COUNT(*) FROM batches b WHERE b.contributor_id=c.id) AS batches,"
                " (SELECT COUNT(*) FROM batches b WHERE b.contributor_id=c.id AND b.status='rejected') AS rejected,"
                " (SELECT COUNT(*) FROM batches b WHERE b.contributor_id=c.id AND b.ts>=?) AS b_day,"
                " (SELECT COALESCE(SUM(rows_ok),0) FROM batches b WHERE b.contributor_id=c.id AND b.ts>=?"
                "   AND b.status='published') AS r_day,"
                " (SELECT MAX(ts) FROM batches b WHERE b.contributor_id=c.id) AS last_ts,"
                " (SELECT COUNT(*) FROM invites v WHERE v.created_by=c.id) AS invites_made"
                " FROM contributors c LEFT JOIN contributors i ON i.id=c.invited_by ORDER BY c.id", (since, since)):
            inviter = ('<span class="dim">operator</span>' if r["invited_by"] is None
                       else self.contributor_link(r["invited_by"], r["inviter"]))
            rej = f'<span class="{"warn" if r["rejected"] else "dim"}">{r["rejected"]}</span>'
            rows.append([r["id"], self.contributor_link(r["id"], r["name"]), status(r["status"]), inviter,
                         esc(when(r["created_at"])), esc(ago(r["last_ts"])), r["samples"],
                         f'{r["batches"]} ({rej})', meter(r["b_day"], r["max_batches_day"]),
                         meter(r["r_day"], r["max_rows_day"]), f'{r["invites_made"]}/{r["max_invites"]}'])
        contributors = table(["id", "name", "status", "invited by", "joined", "last session", "entries",
                              "sessions (rejected)", "sessions / day", "entries / day", "invites"],
                             rows, num=(0, 6), wrap=(1,))
        manage = ""
        if self.can_write:
            manage = ("<h2>Manage</h2><div class=\"panel\">"
                      + self.form("invite", user, "Mint an invite")
                      + self.form("add", user, "Add contributor",
                                  '<input type="text" name="name" maxlength="40" placeholder="name" required>')
                      + '</div><p class="dim">An invite code or a new token is shown once, on the page you land on '
                        'after pressing the button.</p>')
        return self.pg("Community overlay", flash + f'<div class="cards">{cards}</div>' + manage
                       + "<h2>Contributors</h2>" + contributors
                       + "<h2>Recent sessions</h2>" + SESSION_NOTE + self.batch_table(None, 30)
                       + "<h2>Invites</h2>" + self.invite_table(user) + "<h2>Activity</h2>" + self.activity_table(),
                       user=user)

    def batch_table(self, contributor_id, limit):
        where, args = ("WHERE b.contributor_id=?", [contributor_id]) if contributor_id else ("", [])
        rows = []
        for b in self.q("SELECT b.*, c.name FROM batches b JOIN contributors c ON c.id=b.contributor_id "
                        + where + " ORDER BY b.id DESC LIMIT ?", args + [limit]):
            rows.append([f'<a href="{esc(self.base)}/batch/{b["id"]}">#{b["id"]}</a>', esc(when(b["ts"])),
                         self.contributor_link(b["contributor_id"], b["name"]), status(b["status"]),
                         b["rows_in"], b["rows_ok"], detail_summary(b["detail"])])
        return table(["session", "when", "contributor", "status", "entries in", "accepted", "checks"],
                     rows, num=(4, 5), wrap=(2,))

    def invite_table(self, user=""):
        now = int(time.time())
        rows = []
        for v in self.q("SELECT v.*, a.name AS maker, u.name AS taker FROM invites v"
                        " LEFT JOIN contributors a ON a.id=v.created_by LEFT JOIN contributors u ON u.id=v.used_by"
                        " ORDER BY v.created_at DESC LIMIT 40"):
            void = ""
            if v["used_by"] is not None:
                state = f'used by {self.contributor_link(v["used_by"], v["taker"])}'
            elif v["expires_at"] < now:
                state = '<span class="dim">expired / voided</span>'
            else:
                state = '<span class="ok">open</span>'
                if self.can_write:
                    void = self.form("void-invite", user, "Void", extra={"code_hash": v["code_hash"]})
            maker = ('<span class="dim">operator</span>' if v["created_by"] is None
                     else self.contributor_link(v["created_by"], v["maker"]))
            rows.append([esc(when(v["created_at"])), maker, state, esc(when(v["expires_at"]))]
                        + ([void] if self.can_write else []))
        return table(["created", "by", "state", "expires"] + (["" ] if self.can_write else []), rows)

    def activity_table(self):
        try:
            logged = self.q("SELECT * FROM admin_log ORDER BY id DESC LIMIT 40")
        except sqlite3.OperationalError:      # a database from before the log existed
            logged = []
        rows = [[esc(when(r["ts"])), esc(r["actor"]), esc(r["action"]), esc(r["target"]), esc(r["detail"])]
                for r in logged]
        return table(["when", "who", "action", "target", "detail"], rows)

    def contributor(self, cid, user="", flash=""):
        rs = self.q("SELECT c.*, i.name AS inviter FROM contributors c LEFT JOIN contributors i ON i.id=c.invited_by"
                    " WHERE c.id=?", (cid,))
        if not rs:
            return None
        c = rs[0]
        kids = self.q("SELECT id, name, status FROM contributors WHERE invited_by=? ORDER BY id", (cid,))
        info = table(["field", "value"], [
            ["status", status(c["status"])],
            ["joined", esc(when(c["created_at"]))],
            ["invited by", '<span class="dim">operator</span>' if c["invited_by"] is None
             else self.contributor_link(c["invited_by"], c["inviter"])],
            ["limits", esc(f'{c["max_rows_day"]} entries/day · {c["max_batches_day"]} sessions/day · {c["max_invites"]} invites')],
            ["invited", ", ".join(f'{self.contributor_link(k["id"], k["name"])} ({esc(k["status"])})'
                                  for k in kids) or '<span class="dim">nobody</span>'],
        ])
        crumbs = f'<a href="{esc(self.base)}/">overview</a> › {esc(c["name"])}'
        actions = ""
        if self.can_write:
            ident = {"id": c["id"]}
            num = lambda name, label, val: (f'<label>{label} <input type="number" name="{name}" min="0" max="{MAX_LIMIT}" '
                                            f'value="{int(val)}" required></label>')
            panel = [
                self.form("rename", user, "Rename", f'<input type="text" name="name" maxlength="40" required '
                          f'value="{esc(c["name"])}">', extra=ident),
                self.form("limits", user, "Save limits",
                          num("rows", "entries/day", c["max_rows_day"]) + num("batches", "sessions/day", c["max_batches_day"])
                          + num("invites", "invites", c["max_invites"]), extra=ident),
            ]
            if c["status"] == "active":
                panel.append(self.link_button(f'/confirm/rotate?id={c["id"]}', "Rotate token…"))
                panel.append(self.link_button(f'/confirm/ban?id={c["id"]}', "Ban…", "danger"))
                if kids:
                    panel.append(self.link_button(f'/confirm/ban?id={c["id"]}&cascade=1',
                                                  "Ban with everyone they invited…", "danger"))
            else:
                panel.append(self.form("reinstate", user, "Reinstate", extra=ident))
            actions = '<h2>Manage</h2><div class="panel">' + "".join(panel) + "</div>"
        return self.pg(f'Contributor {c["name"]}', flash + "<h2>Contributor</h2>" + info + actions
                       + "<h2>Sessions</h2>" + self.batch_table(cid, 100), crumbs, user)

    def batch(self, bid, user="", flash=""):
        rs = self.q("SELECT b.*, c.name FROM batches b JOIN contributors c ON c.id=b.contributor_id WHERE b.id=?", (bid,))
        if not rs:
            return None
        b = rs[0]
        pts = self.q("SELECT ts_ms, lat_e6, lon_e6, replied, rssi, snr, dl_rssi, responder FROM samples"
                     " WHERE batch_id=? ORDER BY ts_ms", (bid,))
        crumbs = (f'<a href="{esc(self.base)}/">overview</a> › {self.contributor_link(b["contributor_id"], b["name"])} › #{b["id"]}')
        head = table(["field", "value"], [
            ["contributor", self.contributor_link(b["contributor_id"], b["name"])],
            ["status", status(b["status"])], ["shared", esc(when(b["ts"]))],
            ["entries in / accepted", f'{b["rows_in"]} / {b["rows_ok"]}'],
            ["file sha256", f'<span class="dim">{esc(b["file_sha256"])}</span>'],
            ["checks", detail_summary(b["detail"])],
        ])
        body = flash + "<h2>Session</h2>" + head
        if self.can_write and b["status"] == "published":
            body += ('<div class="panel">' + self.link_button(f'/confirm/remove-batch?id={b["id"]}',
                                                              "Remove this session…", "danger") + "</div>")
        if not pts:
            body += ('<p class="dim">No entries stored for this session '
                     f'({"it was removed" if b["status"] == "removed" else "it was rejected, nothing was kept"}).</p>')
        else:
            body += "<h2>Readings</h2>" + self.stats(pts) + "<h2>Where</h2>" + self.plot(pts)
        return self.pg(f"Session #{b['id']}", body, crumbs, user)

    # -- confirmation pages: say exactly what a destructive action will do; GET, so nothing changes here --

    def confirm(self, action, ident, cascade, user=""):
        if action in ("ban", "rotate"):
            rs = self.q("SELECT * FROM contributors WHERE id=?", (ident,))
            if not rs:
                return None
            c = rs[0]
            back = f'<a class="btn" href="{esc(self.base)}/contributor/{c["id"]}">Cancel</a>'
            if c["status"] != "active":
                return self.pg("Not available", f'<p>{esc(c["name"])} is banned, so there is nothing to do here. {back}</p>',
                               user=user)
            if action == "rotate":
                body = (f'<h2>Rotate the token of {esc(c["name"])}?</h2>'
                        "<p>A new token is created and shown once. The current token stops working immediately, "
                        "so the app or browser using it must be given the new one. Their data and invites are untouched.</p>"
                        + self.form("rotate", user, "Rotate token", cls="danger", extra={"id": c["id"]}, confirm=True)
                        + " " + back)
                return self.pg("Rotate token", body, user=user)
            ids = [r[0] for r in self.q(
                "WITH RECURSIVE tree(id) AS (SELECT ?1 UNION SELECT c.id FROM contributors c"
                " JOIN tree t ON c.invited_by = t.id) SELECT id FROM tree", (ident,))] if cascade else [ident]
            rows, total = [], 0
            for cid in ids:
                x = self.q("SELECT name, status, (SELECT COUNT(*) FROM samples s WHERE s.contributor_id=c.id) AS n"
                           " FROM contributors c WHERE id=?", (cid,))[0]
                total += x["n"]
                rows.append([self.contributor_link(cid, x["name"]), status(x["status"]), x["n"]])
            what = ("everyone below them in the invite chain as well" if cascade and len(ids) > 1
                    else "only them" if not cascade else "only them, they have not invited anyone")
            body = (f'<h2>Ban {esc(c["name"])}{" and the people they invited" if cascade else ""}?</h2>'
                    f"<p>Affects {what}. Each is locked out and <b>everything they shared is deleted</b> "
                    f"({total} entries in all). Their unused invites are voided. This cannot be undone, "
                    "although a ban can be lifted (the data stays gone).</p>"
                    + table(["name", "status", "entries to delete"], rows, num=(2,))
                    + '<div class="panel">'
                    + self.form("ban", user, "Ban" + (" all of these" if len(ids) > 1 else ""), cls="danger",
                                extra={"id": c["id"], "cascade": "1" if cascade else ""}, confirm=True)
                    + back + "</div>")
            return self.pg("Ban contributor", body, user=user)
        if action == "remove-batch":
            rs = self.q("SELECT b.*, c.name, (SELECT COUNT(*) FROM samples s WHERE s.batch_id=b.id) AS n"
                        " FROM batches b JOIN contributors c ON c.id=b.contributor_id WHERE b.id=?", (ident,))
            if not rs:
                return None
            b = rs[0]
            back = f'<a class="btn" href="{esc(self.base)}/batch/{b["id"]}">Cancel</a>'
            if b["status"] != "published":
                return self.pg("Not available", f'<p>Session #{b["id"]} is {esc(STATUS_LABEL.get(b["status"], b["status"]))}: nothing is on the map. {back}</p>',
                               user=user)
            body = (f'<h2>Remove session #{b["id"]} by {esc(b["name"])}?</h2>'
                    f'<p>Its {b["n"]} entries are deleted from the map. The contributor keeps their account, '
                    "and the same file can be shared again. This cannot be undone.</p>"
                    + '<div class="panel">'
                    + self.form("remove-batch", user, "Remove session", cls="danger", extra={"id": b["id"]}, confirm=True)
                    + back + "</div>")
            return self.pg("Remove session", body, user=user)
        return None

    @staticmethod
    def stats(pts):
        replied = [p for p in pts if p["replied"]]
        rssi = [p["rssi"] for p in replied if p["rssi"] is not None]
        dl = [p["dl_rssi"] for p in replied if p["dl_rssi"] is not None]
        lats = [p["lat_e6"] / 1e6 for p in pts]
        lons = [p["lon_e6"] / 1e6 for p in pts]
        span = (pts[-1]["ts_ms"] - pts[0]["ts_ms"]) / 1000

        def rng(v):
            return "-" if not v else f'{min(v):.0f} / {statistics.median(v):.0f} / {max(v):.0f} dBm'
        return table(["field", "value"], [
            ["entries", f'{len(pts)} ({len(replied)} answered, {len(pts) - len(replied)} unanswered)'],
            ["responders", esc(", ".join(sorted({p["responder"] for p in replied if p["responder"]})[:12]) or "-")],
            ["uplink RSSI min / median / max", esc(rng(rssi))],
            ["downlink RSSI min / median / max", esc(rng(dl))],
            ["time span", esc(f'{when(pts[0]["ts_ms"] / 1000)} → {when(pts[-1]["ts_ms"] / 1000)}  ({span / 3600:.1f} h)')],
            ["bounding box", esc(f'{min(lats):.5f},{min(lons):.5f} → {max(lats):.5f},{max(lons):.5f}')],
        ])

    @staticmethod
    def plot(pts):
        """The points on a flat equirectangular scatter -- no basemap, so it needs no network."""
        W, H, PAD = 900, 520, 24
        shown = pts if len(pts) <= PLOT_MAX_POINTS else pts[::math.ceil(len(pts) / PLOT_MAX_POINTS)]
        lats = [p["lat_e6"] / 1e6 for p in pts]
        lons = [p["lon_e6"] / 1e6 for p in pts]
        mid = math.cos(math.radians((min(lats) + max(lats)) / 2))
        w = max((max(lons) - min(lons)) * mid, 1e-6)
        h = max(max(lats) - min(lats), 1e-6)
        k = min((W - 2 * PAD) / w, (H - 2 * PAD) / h)
        ox = (W - w * k) / 2
        oy = (H - h * k) / 2

        def colour(r):          # -120 dBm blue -> -50 dBm red, the same span the map's heatmap uses
            t = 0.0 if r is None else min(1.0, max(0.0, (r + 120) / 70))
            return f"hsl({round(240 - 240 * t)},85%,52%)"
        dots = []
        for p in shown:
            x = ox + (p["lon_e6"] / 1e6 - min(lons)) * mid * k
            y = H - oy - (p["lat_e6"] / 1e6 - min(lats)) * k
            if p["replied"]:
                dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{colour(p["rssi"])}" fill-opacity=".85"/>')
            else:
                dots.append(f'<path d="M{x - 3:.1f} {y - 3:.1f}l6 6m0-6l-6 6" stroke="#888" stroke-width="1.2"/>')
        note = f' (every {math.ceil(len(pts) / PLOT_MAX_POINTS)}th of {len(pts)} shown)' if len(shown) < len(pts) else ""
        legend = ('<p class="legend">uplink RSSI'
                  + "".join(f'<i style="background:{colour(r)}"></i>{r}' for r in (-120, -100, -85, -70, -50))
                  + f'<span style="margin-left:12px">✕ unanswered</span>{esc(note)} · north is up, no basemap</p>')
        return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Plot of the session\'s entries">{"".join(dots)}</svg>' + legend


DESTRUCTIVE = {"ban", "rotate", "remove-batch"}      # these only run from their confirmation page
ACTIONS = {"invite", "add", "rename", "limits", "rotate", "ban", "reinstate", "remove-batch", "void-invite"}


class Handler(BaseHTTPRequestHandler):
    server_version = "overlay-admin-web"
    # Filled in per server by make_server() on a subclass, so two servers never share state.
    view = None
    store = None              # None: read-only instance, every POST is refused
    allowed_hosts = ()
    base = ""                 # URL prefix the pages are served below ("" = at the root)
    require_user = False      # public mode: refuse requests the proxy did not authenticate

    def _user(self):
        return (self.headers.get("X-Remote-User") or "").strip()[:80]

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=()):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # "private" so a shared cache between us and the browser never stores these pages.
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("X-Robots-Tag", "noindex")
        # Nothing on these pages needs script; forms may only post back to this same origin; nothing
        # may be framed. Referrer "same-origin" (not "no-referrer") so the browser still sends a
        # truthful Origin header on form posts, which the checks below rely on.
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _page(self, title, body, user=""):
        return page(title, body, base=self.base, user=user, readonly=self.store is None)

    def _redirect(self, path, msg=None, n=None):
        loc = self.base + path
        if msg:
            loc += "?msg=" + msg + (f"&n={int(n)}" if n is not None else "")
        self.send_response(303)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store, private")
        self.end_headers()

    def _prepare(self):
        """Checks every request gets, GET or POST. Returns (user, path below the prefix), or None
        after having answered."""
        # A page on the open web can make the operator's browser request 127.0.0.1:<port>
        # (DNS rebinding). Only answer requests addressed to the names we were started with.
        if (self.headers.get("Host") or "").lower() not in self.allowed_hosts:
            self._send(421, "wrong host", "text/plain")
            return None
        user = self._user()
        # Public mode: nginx authenticates and forwards the name. It overwrites any
        # X-Remote-User the client sent, and $remote_user is empty when auth_basic is off,
        # so a missing header means the proxy did not authenticate this request -- fail closed.
        if self.require_user and not user:
            self._send(403, "forbidden: the proxy did not authenticate this request "
                            "(is auth_basic enabled for this location?)", "text/plain")
            return None
        # urlsplit, not urlparse: the latter strips ";params" off the last segment, so
        # /batch/1;junk would have been served as /batch/1 instead of being a 404.
        path = urlsplit(self.path).path
        if self.base:
            if path == self.base:
                self.send_response(302)
                self.send_header("Location", self.base + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if not path.startswith(self.base + "/"):
                self._send(404, self._page("Not found", "<p>No such page.</p>", user))
                return None
            path = path[len(self.base):]
        return user, path

    def do_GET(self):
        prep = self._prepare()
        if prep is None:
            return
        user, path = prep
        parts = [p for p in path.split("/") if p]
        query = parse_qs(urlsplit(self.path).query)
        flash = ""
        if query.get("msg"):
            n = query.get("n", [""])[0]
            flash = View.flash_html(query["msg"][0], int(n) if n.isdigit() and len(n) < 10 else None)
        try:
            if not parts:
                out = self.view.overview(user, flash)
            elif len(parts) == 2 and parts[0] == "contributor" and parts[1].isdigit():
                out = self.view.contributor(int(parts[1]), user, flash)
            elif len(parts) == 2 and parts[0] == "batch" and parts[1].isdigit():
                out = self.view.batch(int(parts[1]), user, flash)
            elif (self.store is not None and len(parts) == 2 and parts[0] == "confirm"
                  and parts[1] in DESTRUCTIVE and query.get("id", [""])[0].isdigit()):
                out = self.view.confirm(parts[1], int(query["id"][0]), query.get("cascade", [""])[0] == "1", user)
            else:
                out = None
        except sqlite3.Error as e:
            return self._send(500, self._page("Database error", f'<p class="bad">{esc(e)}</p>', user))
        if out is None:
            return self._send(404, self._page("Not found", "<p>No such page.</p>", user))
        self._send(200, out)

    # -- changes ------------------------------------------------------------------------

    def _refuse(self):
        self.send_response(405)
        self.send_header("Allow", "GET, POST" if self.store is not None else "GET")
        self.send_header("Content-Length", "0")
        self.end_headers()
    do_PUT = do_DELETE = do_PATCH = _refuse

    def _same_origin(self):
        """The browser says where a form post came from; anything but this site is refused. A client
        that sends neither header (curl, the tests) is let through here and still needs the CSRF token."""
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site:
            return site in ("same-origin", "none")
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return urlsplit(origin).netloc.lower() == (self.headers.get("Host") or "").lower() and origin != "null"

    def _audit(self, user, action, target, detail=""):
        who = user or "local"
        sys.stderr.write("[admin-web] %s did %s %s %s\n" % (who, action, target, detail))
        try:
            self.store.log_admin(who, action, target, detail)
        except sqlite3.Error as e:           # the change itself already happened; do not hide that
            sys.stderr.write("[admin-web] could not write the activity log: %s\n" % e)

    def _secret_page(self, user, title, label, secret, note, back):
        body = (f"<h2>{esc(title)}</h2><p>{esc(note)}</p><p class=\"dim\">{esc(label)}</p>"
                f'<pre class="secret">{esc(secret)}</pre>'
                '<p class="warn">Shown only once, here. Copy it now; do not reload this page '
                "(that would create another one).</p>" + f'<p><a class="btn" href="{esc(self.base)}{esc(back)}">Back</a></p>')
        return 200, self._page(title, body, user)

    def _error(self, user, code, message):
        return code, self._page("Not done", f'<p class="bad">{esc(message)}</p><p><a class="btn" '
                                            f'href="{esc(self.base)}/">Back to the overview</a></p>', user)

    def do_POST(self):
        prep = self._prepare()
        if prep is None:
            return
        user, path = prep
        parts = [p for p in path.split("/") if p]
        if self.store is None or len(parts) != 2 or parts[0] != "do":
            return self._refuse()
        action = parts[1]
        if action not in ACTIONS:
            return self._send(404, self._page("Not found", "<p>No such action.</p>", user))
        if not self._same_origin():
            return self._send(403, self._page("Refused", "<p>This request did not come from this page.</p>", user))
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._send(411, "length required", "text/plain")
        if not 0 <= length <= MAX_FORM_BYTES:
            return self._send(413, "too large", "text/plain")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
            return self._send(415, "form posts only", "text/plain")
        try:
            raw = self.rfile.read(length).decode("utf-8", "replace")
            form = {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True, max_num_fields=20).items()}
        except ValueError:
            return self._send(400, "bad form", "text/plain")
        if not self.view.csrf_ok(user, form.get("csrf")):
            return self._send(403, self._page(
                "Refused", "<p>The form is out of date (or was not made by this page). Go back, reload it and try again.</p>",
                user))
        if action in DESTRUCTIVE and form.get("confirm") != "1":
            return self._send(400, self._page("Not done", "<p>Open this action from its confirmation page.</p>", user))
        try:
            code, out = self._do(action, form, user)
        except StoreError as e:
            c = e.args[0]
            code, out = self._error(user, 404 if c.startswith("no_such") else 400 if c.startswith("bad") else 409,
                                    ERRORS.get(c, "Refused."))
        except sqlite3.Error as e:
            code, out = self._error(user, 500, f"Database error: {e}")
        if code == 303:
            return self._redirect(*out)
        self._send(code, out)

    def _do(self, action, form, user):
        """Runs one action. Returns (200, page) or (303, redirect arguments); a refusal raises StoreError."""
        st = self.store

        def ident():
            v = (form.get("id") or "").strip()
            if not v.isdigit() or len(v) > 12:
                raise StoreError("no_such_contributor" if action != "remove-batch" else "no_such_batch")
            return int(v)

        def whole(name):
            v = (form.get(name) or "").strip()
            if not v.isdigit() or len(v) > 12:
                raise StoreError("bad_limit")
            return int(v)

        if action == "invite":
            code = st.create_invite(None)
            self._audit(user, "invite", "operator invite")
            return self._secret_page(user, "Invite code", "Give this to the person; they redeem it on the map page.",
                                     code, f"Valid for {st.cfg['invite_ttl_days']} days, one use.", "/")
        if action == "add":
            cid, token = st.create_contributor(form.get("name"))
            self._audit(user, "add", f"contributor {cid}", st.get_contributor(cid)["name"])
            return self._secret_page(user, "Contributor created", f"Access token for contributor {cid}",
                                     token, "Hand this over privately; it is their password.", f"/contributor/{cid}")
        if action == "rename":
            cid = ident()
            before = (st.get_contributor(cid) or {}).get("name", "")
            name = st.rename(cid, form.get("name"))
            self._audit(user, "rename", f"contributor {cid}", f"{before} -> {name}")
            return 303, (f"/contributor/{cid}", "renamed", None)
        if action == "limits":
            cid = ident()
            rows, batches, invites = whole("rows"), whole("batches"), whole("invites")
            st.set_limits(cid, max_rows_day=rows, max_batches_day=batches, max_invites=invites)
            self._audit(user, "limits", f"contributor {cid}", f"entries/day={rows} sessions/day={batches} invites={invites}")
            return 303, (f"/contributor/{cid}", "limits", None)
        if action == "rotate":
            cid = ident()
            token = st.rotate_token(cid)
            self._audit(user, "rotate-token", f"contributor {cid}")
            return self._secret_page(user, "New token", f"New access token for contributor {cid}", token,
                                     "The previous token no longer works.", f"/contributor/{cid}")
        if action == "ban":
            cid = ident()
            cascade = form.get("cascade") == "1"
            removed = st.ban(cid, cascade)
            self._audit(user, "ban", f"contributor {cid}",
                        ("with cascade; " if cascade else "") + ", ".join(f"{k}: {v} entries" for k, v in removed.items()))
            return 303, (f"/contributor/{cid}", "banned", sum(removed.values()))
        if action == "reinstate":
            cid = ident()
            st.reinstate(cid)
            self._audit(user, "reinstate", f"contributor {cid}")
            return 303, (f"/contributor/{cid}", "reinstated", None)
        if action == "remove-batch":
            bid = ident()
            n = st.remove_batch(bid)
            self._audit(user, "remove-session", f"session {bid}", f"{n} entries")
            return 303, (f"/batch/{bid}", "removed", n)
        if action == "void-invite":
            h = (form.get("code_hash") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", h):
                raise StoreError("no_such_invite")
            st.void_invite(h)
            self._audit(user, "void-invite", "invite", h[:8])
            return 303, ("/", "voided", None)
        raise StoreError("no_such_invite")      # unreachable: ACTIONS is checked first

    def log_message(self, fmt, *args):
        who = self._user() if hasattr(self, "headers") else ""
        sys.stderr.write("[admin-web] %s%s\n" % (f"user={who} " if who else "", fmt % args))


def load_config(config_path):
    try:
        with open(config_path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def overlay_db_path(cfg):
    db = os.path.expanduser(cfg.get("db_path", "~/wardrive_bot.db"))
    return os.path.expanduser(cfg.get("overlay_db_path",
                                      os.path.join(os.path.dirname(db), "wardrive_overlay.db")))


_BASE_RE = re.compile(r"^(/(?!\.+(?:/|$))[A-Za-z0-9._~-]+)+$")   # no "." / ".." segments
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:\d{1,5})?$")


def make_server(db_path, port, host="127.0.0.1", base="", public_hosts=(), store=None):
    """Build the server. base is the URL prefix ("" or "/overlay-admin"); public_hosts are
    the extra Host names accepted when a proxy fronts this, which also turns on the
    requirement that the proxy supplies X-Remote-User. store is the OverlayStore the page
    changes things through; without one the server is read-only and refuses every POST."""
    base = (base or "").rstrip("/")
    if base and not _BASE_RE.match(base):
        raise ValueError(f"bad base path {base!r}: expected something like /overlay-admin")
    hosts = tuple(h.strip().lower() for h in public_hosts)
    for h in hosts:
        if not _HOST_RE.match(h):
            raise ValueError(f"bad public host {h!r}: expected a bare host name such as map.rns.moscow")
    view = View(db_path, base, csrf_secret=secrets.token_bytes(32), can_write=store is not None)
    handler = type("Handler", (Handler,), {"view": view, "store": store, "base": base,
                                          "require_user": bool(hosts), "allowed_hosts": ()})
    srv = ThreadingHTTPServer((host, port), handler)
    bound = srv.server_port            # differs from `port` when 0 asked the OS to choose
    handler.allowed_hosts = (f"127.0.0.1:{bound}", f"localhost:{bound}", f"[::1]:{bound}") + hosts
    return srv


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--db", help="overlay database (default: from the config)")
    ap.add_argument("--port", type=int, help="default 8850")
    ap.add_argument("--base-path", help="serve below this URL prefix, e.g. /overlay-admin")
    ap.add_argument("--read-only", action="store_true",
                    help="only show things; refuse every change (also: \"read_only\": true in the config)")
    ap.add_argument("--public-host", action="append", metavar="NAME",
                    help="Host name nginx forwards, e.g. map.rns.moscow (repeatable). Turns on the\n"
                         "requirement that every request carries nginx's X-Remote-User.")
    a = ap.parse_args()
    cfg = load_config(a.config)
    # Flags win; otherwise the "overlay_admin" block of wardrive_map.json, so a service
    # file does not have to carry host-specific values.
    ow = cfg.get("overlay_admin") or {}
    port = a.port or ow.get("port", 8850)
    base = a.base_path if a.base_path is not None else ow.get("base_path", "")
    hosts = a.public_host or ow.get("public_hosts") or []
    db = a.db or overlay_db_path(cfg)
    if not os.path.exists(db):
        sys.exit(f"no overlay database at {db} (is overlay_enabled set, and has the server started once?)")
    read_only = a.read_only or bool(ow.get("read_only"))
    # The same limits as the server and the CLI use, so a contributor added here gets the same defaults.
    store = None if read_only else OverlayStore(db, **(cfg.get("overlay") or {}))
    try:
        srv = make_server(db, port, base=base, public_hosts=hosts, store=store)   # loopback only: no option to widen this
    except ValueError as e:
        sys.exit(f"error: {e}")
    mode = (f"behind a proxy as {', '.join(hosts)}{base.rstrip('/')}/ (requires X-Remote-User)"
            if hosts else "on loopback")
    print(f"Overlay admin ({'read-only' if read_only else 'can make changes'}) on 127.0.0.1:{port}, {mode}  "
          f"(database {db}). Ctrl+C to stop.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
