"""
overlay_store.py - storage, contributors, quotas and takedown for the community overlay.

Lives in its own SQLite file, never the bot's: the map only reads that one, and a flood
of uploads must not be able to touch live collection data. Shared by map_server.py (the
upload routes) and overlay_admin.py (the operator's CLI).

Every sample belongs to a batch and every batch to a contributor, so any upload or any
person can be removed in one operation. Tokens and invite codes are stored only as
SHA-256: they are 256/128-bit random strings, so there is nothing to brute-force and
nothing worth stealing from a database dump.
"""

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager

DEFAULTS = {
    "max_rows_day":    20000,     # accepted samples per contributor per rolling 24 h
    "max_batches_day": 10,        # uploads per contributor per rolling 24 h, rejected ones too
    "max_invites":     3,         # invites a contributor may hand out in total
    "invite_ttl_days": 14,
    "max_total_rows":  5000000,   # whole-overlay ceiling; bounds disk against many accounts
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contributors (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    invited_by      INTEGER REFERENCES contributors(id),
    status          TEXT NOT NULL DEFAULT 'active',      -- active | banned
    created_at      INTEGER NOT NULL,
    max_rows_day    INTEGER NOT NULL,
    max_batches_day INTEGER NOT NULL,
    max_invites     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    code_hash  TEXT PRIMARY KEY,
    created_by INTEGER REFERENCES contributors(id),      -- NULL: minted by the operator
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_by    INTEGER REFERENCES contributors(id)
);
CREATE TABLE IF NOT EXISTS batches (
    id             INTEGER PRIMARY KEY,
    contributor_id INTEGER NOT NULL REFERENCES contributors(id),
    ts             INTEGER NOT NULL,
    file_sha256    TEXT NOT NULL,
    rows_in        INTEGER NOT NULL,
    rows_ok        INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL,                        -- published | rejected | removed
    detail         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS batches_file ON batches(contributor_id, file_sha256)
    WHERE status IN ('published', 'removed');
CREATE INDEX IF NOT EXISTS batches_contrib_ts ON batches(contributor_id, ts);
CREATE TABLE IF NOT EXISTS samples (
    id             INTEGER PRIMARY KEY,
    batch_id       INTEGER NOT NULL REFERENCES batches(id),
    contributor_id INTEGER NOT NULL REFERENCES contributors(id),
    dedup_key      TEXT NOT NULL,
    ts_ms          INTEGER NOT NULL,
    lat_e6         INTEGER NOT NULL,
    lon_e6         INTEGER NOT NULL,
    replied        INTEGER NOT NULL,
    responder      TEXT NOT NULL,
    rssi           REAL, snr REAL, dl_rssi REAL, dl_snr REAL,
    accuracy_m     REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS samples_dedup ON samples(contributor_id, dedup_key);
CREATE INDEX IF NOT EXISTS samples_batch ON samples(batch_id);
CREATE INDEX IF NOT EXISTS samples_lat   ON samples(lat_e6);
CREATE INDEX IF NOT EXISTS samples_ts    ON samples(ts_ms);
CREATE TABLE IF NOT EXISTS admin_log (
    id     INTEGER PRIMARY KEY,
    ts     INTEGER NOT NULL,
    actor  TEXT NOT NULL,          -- who did it: the name the admin page's proxy authenticated
    action TEXT NOT NULL,
    target TEXT NOT NULL,          -- e.g. "contributor 3" or "batch 7"
    detail TEXT NOT NULL DEFAULT ''
);
"""

# Public output: ~1 m positions and hour-resolution times. A heatmap needs neither more,
# and a route traced to the centimetre and the minute says where someone lives.
PUBLIC_COORD_DECIMALS = 5
PUBLIC_TS_STEP = 3600


class StoreError(Exception):
    """Refused for a reason the caller reports. args[0] is a short machine-readable code."""


def hash_secret(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# Largest value the operator may set for a per-contributor limit. Far above anything sensible; it only
# stops a typo from storing a number that overflows or means "unlimited" by accident.
MAX_LIMIT = 10_000_000


def _clean_name(name):
    name = "".join(ch for ch in str(name or "") if ch.isprintable()).strip()[:40]
    if not name:
        raise StoreError("bad_name")
    return name


class OverlayStore:
    def __init__(self, path, **overrides):
        self.path = path
        self.cfg = {**DEFAULTS, **{k: v for k, v in overrides.items() if k in DEFAULTS}}
        # executescript commits whatever transaction is open, so it gets a connection of its own.
        c = sqlite3.connect(self.path, timeout=15)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)
        finally:
            c.close()

    @contextmanager
    def _tx(self):
        # One short-lived connection per operation: the server runs 8 threads, and
        # BEGIN IMMEDIATE serialises the writers so quota checks cannot race.
        c = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()

    @contextmanager
    def _read(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    # ---- contributors & invites ------------------------------------------------

    def _new_contributor(self, c, name, invited_by):
        token = "wd_" + secrets.token_urlsafe(32)
        cur = c.execute(
            "INSERT INTO contributors (name, token_hash, invited_by, created_at, max_rows_day,"
            " max_batches_day, max_invites) VALUES (?,?,?,?,?,?,?)",
            (_clean_name(name), hash_secret(token), invited_by, int(time.time()),
             self.cfg["max_rows_day"], self.cfg["max_batches_day"], self.cfg["max_invites"]))
        return cur.lastrowid, token

    def create_contributor(self, name, invited_by=None):
        """Operator path: a contributor with no invite. Returns (id, token); token shown once."""
        with self._tx() as c:
            return self._new_contributor(c, name, invited_by)

    def create_invite(self, created_by=None):
        """Returns the code, shown once. created_by None is the operator (no quota)."""
        code = "inv_" + secrets.token_urlsafe(16)
        now = int(time.time())
        with self._tx() as c:
            if created_by is not None:
                who = c.execute("SELECT status, max_invites FROM contributors WHERE id=?",
                                (created_by,)).fetchone()
                if not who or who["status"] != "active":
                    raise StoreError("forbidden")
                made = c.execute("SELECT COUNT(*) FROM invites WHERE created_by=?",
                                 (created_by,)).fetchone()[0]
                if made >= who["max_invites"]:
                    raise StoreError("invite_quota")
            c.execute("INSERT INTO invites (code_hash, created_by, created_at, expires_at)"
                      " VALUES (?,?,?,?)",
                      (hash_secret(code), created_by, now,
                       now + self.cfg["invite_ttl_days"] * 86400))
        return code

    def redeem_invite(self, code, name):
        """Invite code -> (contributor id, token). One use; void if the inviter was banned."""
        now = int(time.time())
        with self._tx() as c:
            inv = c.execute("SELECT * FROM invites WHERE code_hash=?",
                            (hash_secret(str(code)),)).fetchone()
            if not inv or inv["used_by"] is not None or inv["expires_at"] < now:
                raise StoreError("bad_invite")
            if inv["created_by"] is not None:
                who = c.execute("SELECT status FROM contributors WHERE id=?",
                                (inv["created_by"],)).fetchone()
                if not who or who["status"] != "active":
                    raise StoreError("bad_invite")
            cid, token = self._new_contributor(c, name, inv["created_by"])
            c.execute("UPDATE invites SET used_by=? WHERE code_hash=?", (cid, inv["code_hash"]))
            return cid, token

    def authenticate(self, token):
        """Bearer token -> contributor row (dict), or None. Banned contributors fail."""
        if not token or len(token) > 200:
            return None
        with self._read() as c:
            row = c.execute("SELECT * FROM contributors WHERE token_hash=? AND status='active'",
                            (hash_secret(token),)).fetchone()
        return dict(row) if row else None

    # ---- uploads ---------------------------------------------------------------

    def _usage(self, c, cid):
        since = int(time.time()) - 86400
        batches = c.execute("SELECT COUNT(*) FROM batches WHERE contributor_id=? AND ts>=?",
                            (cid, since)).fetchone()[0]
        rows = c.execute("SELECT COALESCE(SUM(rows_ok),0) FROM batches WHERE contributor_id=?"
                         " AND ts>=? AND status='published'", (cid, since)).fetchone()[0]
        return batches, rows

    def check_batch_quota(self, contributor):
        """Cheap pre-check, before the body is read and parsed."""
        with self._read() as c:
            batches, _ = self._usage(c, contributor["id"])
        if batches >= contributor["max_batches_day"]:
            raise StoreError("quota_batches")

    def quota_status(self, contributor):
        """What this contributor can still do today, for the app's "Check connection" and for
        greying out Share before a doomed upload. Read-only; same counters check_batch_quota
        and add_upload enforce, so it cannot disagree with them."""
        with self._read() as c:
            batches, rows = self._usage(c, contributor["id"])
            made = c.execute("SELECT COUNT(*) FROM invites WHERE created_by=?",
                             (contributor["id"],)).fetchone()[0]
        return {
            "name": contributor["name"],
            "rows_left_today": max(0, contributor["max_rows_day"] - rows),
            "batches_left_today": max(0, contributor["max_batches_day"] - batches),
            "invites_left": max(0, contributor["max_invites"] - made),
        }

    def add_upload(self, contributor, file_sha256, result):
        """Record an upload (a validate_batch BatchResult) and, if it passed, publish it.

        Returns {"batch": id, "accepted": n, "duplicates": n, "rejected": {...}}.
        Raises StoreError for quota, a repeated file, or a full overlay. A batch that
        failed validation is recorded as 'rejected' (it still counts toward the daily
        batch quota, so probing the validator is not free) and reported via result.
        """
        cid = contributor["id"]
        now = int(time.time())
        detail = {"rejected": dict(result.rejected), "duplicates": result.duplicates}
        with self._tx() as c:
            who = c.execute("SELECT status, max_rows_day, max_batches_day FROM contributors"
                            " WHERE id=?", (cid,)).fetchone()
            if not who or who["status"] != "active":
                raise StoreError("forbidden")
            batches, rows_today = self._usage(c, cid)
            if batches >= who["max_batches_day"]:
                raise StoreError("quota_batches")
            if not result.batch_error and rows_today + len(result.rows) > who["max_rows_day"]:
                raise StoreError("quota_rows")
            if c.execute("SELECT 1 FROM batches WHERE contributor_id=? AND file_sha256=?"
                         " AND status IN ('published','removed')", (cid, file_sha256)).fetchone():
                raise StoreError("duplicate_file")
            if not result.batch_error:
                total = c.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
                if total + len(result.rows) > self.cfg["max_total_rows"]:
                    raise StoreError("overlay_full")

            status = "rejected" if result.batch_error else "published"
            if result.batch_error:
                detail["error"] = result.batch_error
            bid = c.execute(
                "INSERT INTO batches (contributor_id, ts, file_sha256, rows_in, status, detail)"
                " VALUES (?,?,?,?,?,?)",
                (cid, now, file_sha256, result.raw_count, status,
                 json.dumps(detail))).lastrowid
            accepted = 0
            if not result.batch_error:
                cur = c.executemany(
                    "INSERT OR IGNORE INTO samples (batch_id, contributor_id, dedup_key, ts_ms,"
                    " lat_e6, lon_e6, replied, responder, rssi, snr, dl_rssi, dl_snr, accuracy_m)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(bid, cid, r["dedup_key"], r["ts_ms"], r["lat_e6"], r["lon_e6"],
                      int(r["replied"]), r["responder"], r["rssi"], r["snr"], r["dl_rssi"],
                      r["dl_snr"], r["accuracy_m"]) for r in result.rows])
                accepted = cur.rowcount
                dupes_here = len(result.rows) - accepted
                detail["duplicates"] += dupes_here
                if accepted == 0:
                    status = "rejected"
                    detail["error"] = "all_duplicates"
                c.execute("UPDATE batches SET rows_ok=?, status=?, detail=? WHERE id=?",
                          (accepted, status, json.dumps(detail), bid))
        return {"batch": bid, "accepted": accepted, "duplicates": detail["duplicates"],
                "rejected": detail["rejected"], "error": detail.get("error")}

    # ---- reading ---------------------------------------------------------------

    def published(self, bbox=None, since_s=None, limit=20000):
        """Samples to show, newest first. bbox is (west, south, east, north) in degrees."""
        where, args = [], []
        if bbox:
            w, s, e, n = bbox
            where.append("lat_e6 BETWEEN ? AND ?")
            args += [int(s * 1e6), int(n * 1e6)]
            if w <= e:
                where.append("lon_e6 BETWEEN ? AND ?")
                args += [int(w * 1e6), int(e * 1e6)]
            else:                                   # the box crosses the antimeridian
                where.append("(lon_e6 >= ? OR lon_e6 <= ?)")
                args += [int(w * 1e6), int(e * 1e6)]
        if since_s is not None:
            where.append("ts_ms >= ?")
            args.append(int(since_s * 1000))
        sql = ("SELECT ts_ms, lat_e6, lon_e6, replied, rssi, snr, dl_rssi, dl_snr FROM samples"
               + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY ts_ms DESC LIMIT ?")
        with self._read() as c:
            return [tuple(r) for r in c.execute(sql, args + [int(limit)])]

    @staticmethod
    def public_geojson(rows):
        """Rows from published() -> GeoJSON with nothing that identifies who sent them."""
        feats = []
        for ts_ms, lat_e6, lon_e6, replied, rssi, snr, dl_rssi, dl_snr in rows:
            props = {"ts": ts_ms // 1000 // PUBLIC_TS_STEP * PUBLIC_TS_STEP,
                     "replied": bool(replied)}
            for k, v in (("rssi", rssi), ("snr", snr), ("dl_rssi", dl_rssi), ("dl_snr", dl_snr)):
                if v is not None:
                    props[k] = v
            feats.append({"type": "Feature", "properties": props,
                          "geometry": {"type": "Point", "coordinates": [
                              round(lon_e6 / 1e6, PUBLIC_COORD_DECIMALS),
                              round(lat_e6 / 1e6, PUBLIC_COORD_DECIMALS)]}})
        return {"type": "FeatureCollection", "features": feats}

    # ---- operator actions ------------------------------------------------------

    def list_contributors(self):
        with self._read() as c:
            return [dict(r) for r in c.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM samples s WHERE s.contributor_id=c.id) AS rows,"
                " (SELECT COUNT(*) FROM batches b WHERE b.contributor_id=c.id) AS batches"
                " FROM contributors c ORDER BY c.id")]

    def list_batches(self, contributor_id=None, limit=50):
        sql = "SELECT * FROM batches" + (" WHERE contributor_id=?" if contributor_id else "")
        with self._read() as c:
            return [dict(r) for r in c.execute(
                sql + " ORDER BY id DESC LIMIT ?",
                ([contributor_id] if contributor_id else []) + [limit])]

    def _remove_batch(self, c, batch_id):
        n = c.execute("DELETE FROM samples WHERE batch_id=?", (batch_id,)).rowcount
        c.execute("UPDATE batches SET status='removed' WHERE id=? AND status='published'",
                  (batch_id,))
        return n

    def remove_batch(self, batch_id):
        """Take one upload off the map. Returns the number of samples removed."""
        with self._tx() as c:
            if not c.execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone():
                raise StoreError("no_such_batch")
            return self._remove_batch(c, batch_id)

    def ban(self, contributor_id, cascade=False):
        """Lock a contributor out and remove everything they sent. cascade also does the
        same to everyone they invited, and whom those invited. Returns {id: samples removed}."""
        with self._tx() as c:
            if not c.execute("SELECT 1 FROM contributors WHERE id=?", (contributor_id,)).fetchone():
                raise StoreError("no_such_contributor")
            ids = [contributor_id]
            if cascade:
                ids = [r[0] for r in c.execute(
                    "WITH RECURSIVE tree(id) AS (SELECT ?1 UNION SELECT c.id FROM contributors c"
                    " JOIN tree t ON c.invited_by = t.id) SELECT id FROM tree", (contributor_id,))]
            removed = {}
            for cid in ids:
                c.execute("UPDATE contributors SET status='banned' WHERE id=?", (cid,))
                # Unused invites die with their owner; redeem_invite also checks, this keeps the table honest.
                c.execute("UPDATE invites SET expires_at=0 WHERE created_by=? AND used_by IS NULL",
                          (cid,))
                removed[cid] = sum(self._remove_batch(c, b[0]) for b in c.execute(
                    "SELECT id FROM batches WHERE contributor_id=?", (cid,)).fetchall())
            return removed

    def set_limits(self, contributor_id, **limits):
        cols = {k: int(v) for k, v in limits.items()
                if k in ("max_rows_day", "max_batches_day", "max_invites") and v is not None}
        if not cols:
            return
        if any(not 0 <= v <= MAX_LIMIT for v in cols.values()):
            raise StoreError("bad_limit")
        with self._tx() as c:
            if not c.execute("SELECT 1 FROM contributors WHERE id=?", (contributor_id,)).fetchone():
                raise StoreError("no_such_contributor")
            c.execute("UPDATE contributors SET " + ", ".join(f"{k}=?" for k in cols)
                      + " WHERE id=?", list(cols.values()) + [contributor_id])

    # ---- operator actions used by the admin web page -----------------------------

    def get_contributor(self, contributor_id):
        with self._read() as c:
            r = c.execute("SELECT * FROM contributors WHERE id=?", (contributor_id,)).fetchone()
            return dict(r) if r else None

    def rotate_token(self, contributor_id):
        """Give an active contributor a new token; the old one stops working at once.
        Returns the new token, shown once."""
        token = "wd_" + secrets.token_urlsafe(32)
        with self._tx() as c:
            who = c.execute("SELECT status FROM contributors WHERE id=?", (contributor_id,)).fetchone()
            if not who:
                raise StoreError("no_such_contributor")
            if who["status"] != "active":
                raise StoreError("banned")
            c.execute("UPDATE contributors SET token_hash=? WHERE id=?",
                      (hash_secret(token), contributor_id))
        return token

    def reinstate(self, contributor_id):
        """Let a banned contributor back in. What the ban wiped does not come back, and their
        unused invites stay expired."""
        with self._tx() as c:
            who = c.execute("SELECT status FROM contributors WHERE id=?", (contributor_id,)).fetchone()
            if not who:
                raise StoreError("no_such_contributor")
            if who["status"] != "banned":
                raise StoreError("not_banned")
            c.execute("UPDATE contributors SET status='active' WHERE id=?", (contributor_id,))

    def rename(self, contributor_id, name):
        name = _clean_name(name)
        with self._tx() as c:
            if not c.execute("SELECT 1 FROM contributors WHERE id=?", (contributor_id,)).fetchone():
                raise StoreError("no_such_contributor")
            c.execute("UPDATE contributors SET name=? WHERE id=?", (name, contributor_id))
        return name

    def void_invite(self, code_hash):
        """Cancel an invite nobody has used yet."""
        with self._tx() as c:
            inv = c.execute("SELECT used_by FROM invites WHERE code_hash=?", (str(code_hash),)).fetchone()
            if not inv:
                raise StoreError("no_such_invite")
            if inv["used_by"] is not None:
                raise StoreError("invite_used")
            c.execute("UPDATE invites SET expires_at=0 WHERE code_hash=?", (str(code_hash),))

    def log_admin(self, actor, action, target, detail=""):
        with self._tx() as c:
            c.execute("INSERT INTO admin_log (ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
                      (int(time.time()), str(actor)[:80] or "-", str(action)[:40], str(target)[:80],
                       str(detail)[:300]))

    def list_admin_log(self, limit=50):
        with self._read() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM admin_log ORDER BY id DESC LIMIT ?", (int(limit),))]
