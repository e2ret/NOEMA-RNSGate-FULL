#!/opt/reticulum/bin/python3
"""
overlay_admin.py - operator CLI for the community overlay (see map_server.py).

Runs on the map host, against the same overlay database and config as the server. The
same operations are available in the browser through overlay_admin_web.py (behind
Authelia on the public map); this CLI needs only a shell and works when that is down.

    overlay_admin.py invite                       one-use invite code, from the operator
    overlay_admin.py add NAME                     create a contributor directly; prints a token
    overlay_admin.py contributors                 who exists, who invited whom, what they sent
    overlay_admin.py batches [--contributor ID]   recent uploads and why any were rejected
    overlay_admin.py remove-batch ID              take one upload off the map
    overlay_admin.py ban ID [--cascade]           lock out and wipe a contributor (and their invitees)
    overlay_admin.py limits ID [--rows-day N] [--batches-day N] [--invites N]
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from overlay_store import OverlayStore, StoreError

CONFIG_PATH = "/etc/reticulum/wardrive_map.json"


def load_store(config_path):
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    db = os.path.expanduser(cfg.get("db_path", "~/wardrive_bot.db"))
    path = os.path.expanduser(cfg.get("overlay_db_path",
                                      os.path.join(os.path.dirname(db), "wardrive_overlay.db")))
    if not cfg.get("overlay_enabled"):
        print(f"[note] overlay_enabled is not true in {config_path}; the server will not use "
              f"{path} yet", file=sys.stderr)
    return OverlayStore(path, **(cfg.get("overlay") or {})), path


def when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def table(rows, cols):
    if not rows:
        print("(none)")
        return
    width = [max(len(str(c)), *(len(str(r.get(c, ""))) for r in rows)) for c in cols]
    print("  ".join(str(c).ljust(w) for c, w in zip(cols, width)))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(w) for c, w in zip(cols, width)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--config", default=CONFIG_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("invite")
    add = sub.add_parser("add")
    add.add_argument("name")
    sub.add_parser("contributors")
    bt = sub.add_parser("batches")
    bt.add_argument("--contributor", type=int)
    bt.add_argument("--limit", type=int, default=30)
    rb = sub.add_parser("remove-batch")
    rb.add_argument("id", type=int)
    bn = sub.add_parser("ban")
    bn.add_argument("id", type=int)
    bn.add_argument("--cascade", action="store_true")
    lm = sub.add_parser("limits")
    lm.add_argument("id", type=int)
    lm.add_argument("--rows-day", type=int)
    lm.add_argument("--batches-day", type=int)
    lm.add_argument("--invites", type=int)
    a = ap.parse_args()

    store, path = load_store(a.config)
    try:
        if a.cmd == "invite":
            print(store.create_invite(None))
            print("(shown once; valid %d days; the holder redeems it on the map page)"
                  % store.cfg["invite_ttl_days"])
        elif a.cmd == "add":
            cid, token = store.create_contributor(a.name)
            print(f"contributor {cid}\ntoken {token}\n(shown once)")
        elif a.cmd == "contributors":
            rows = store.list_contributors()
            for r in rows:
                r["created"] = when(r["created_at"])
                r["limits"] = f"{r['max_rows_day']}r/{r['max_batches_day']}b/{r['max_invites']}i"
            table(rows, ["id", "name", "status", "invited_by", "created", "batches", "rows", "limits"])
        elif a.cmd == "batches":
            rows = store.list_batches(a.contributor, a.limit)
            for r in rows:
                r["when"] = when(r["ts"])
                d = json.loads(r["detail"] or "{}")
                r["why"] = d.get("error") or ", ".join(f"{k}={v}" for k, v in
                                                       (d.get("rejected") or {}).items()) or ""
            table(rows, ["id", "contributor_id", "when", "status", "rows_in", "rows_ok", "why"])
        elif a.cmd == "remove-batch":
            print(f"removed {store.remove_batch(a.id)} samples")
        elif a.cmd == "ban":
            removed = store.ban(a.id, a.cascade)
            for cid, n in removed.items():
                print(f"contributor {cid}: banned, {n} samples removed")
        elif a.cmd == "limits":
            store.set_limits(a.id, max_rows_day=a.rows_day, max_batches_day=a.batches_day,
                             max_invites=a.invites)
            print("ok")
    except StoreError as e:
        sys.exit(f"error: {e.args[0]}")


if __name__ == "__main__":
    main()
