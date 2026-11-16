#!/usr/bin/env python3
"""Tests for overlay_validate.py and overlay_store.py.   python3 map/test_overlay.py"""
import json
import os
import re
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import overlay_validate as V
from overlay_store import OverlayStore, StoreError, hash_secret

NOW = int(time.time() * 1000)
M_PER_DEG_LAT = 111195.0


def point(i, **over):
    """A believable app sample: driving north at ~10 m/s, one probe every 10 s."""
    p = {"time_ms": NOW - 3600_000 + i * 10_000, "accuracy_m": 5.0, "provider": "gps",
         "session": "a1b2c3d4", "seq": i, "responder_id": "0badf00d", "replied": True,
         "rssi": -90 - (i % 7), "snr": 5.5 - (i % 3), "dl_rssi": -95, "dl_snr": 4.0,
         "lat": 55.75 + i * 100.0 / M_PER_DEG_LAT, "lon": 37.62}
    p.update(over)
    return p


def geojson(points):
    feats = []
    for p in points:
        props = {k: v for k, v in p.items() if k not in ("lat", "lon")}
        feats.append({"type": "Feature", "geometry": {"type": "Point",
                      "coordinates": [p["lon"], p["lat"]]}, "properties": props})
    return json.dumps({"type": "FeatureCollection", "features": feats}).encode()


def run(points, name="x.geojson", lim=None):
    return V.validate_batch(V.parse_export(name, geojson(points)), NOW, lim)


class ValidateTests(unittest.TestCase):
    def test_valid_batch(self):
        r = run([point(i) for i in range(30)])
        self.assertIsNone(r.batch_error)
        self.assertEqual(len(r.rows), 30)
        self.assertEqual(sum(r.rejected.values()), 0)

    def test_unanswered_probe(self):
        miss = point(1, replied=False, responder_id=None, rssi=None, snr=None,
                     dl_rssi=None, dl_snr=None)
        r = run([point(0), miss, point(2)])
        self.assertEqual(len(r.rows), 3)
        self.assertFalse(r.rows[1]["replied"])

    def row_reason(self, **over):
        # 30 good rows plus one bad one stays under the batch threshold.
        r = run([point(i) for i in range(30)] + [point(30, **over)])
        self.assertEqual(len(r.rows), 30, over)
        return list(r.rejected)

    def test_row_rejections(self):
        cases = [
            (dict(time_ms=NOW + 3600_000), "time_future"),
            (dict(time_ms=NOW - 800 * 86400_000), "time_old"),
            (dict(time=None, time_ms=None), "missing_time"),
            (dict(time="2000-01-01T00:00:00Z"), "time_mismatch"),
            (dict(rssi=10), "bad_signal"),
            (dict(rssi=-300), "bad_signal"),
            (dict(snr=99), "bad_signal"),
            (dict(dl_rssi="x"), "bad_signal"),
            (dict(responder_id="nothex!!"), "bad_responder"),
            (dict(responder_id="<script>"), "bad_responder"),
            (dict(replied=False), "inconsistent"),
            (dict(rssi=None), "inconsistent"),
            (dict(lat=0, lon=0), "null_island"),
            (dict(lat=91), "bad_position"),
            (dict(lat=float("nan")), None),
            (dict(accuracy_m=500), "poor_accuracy"),
            (dict(session="zz"), "bad_session"),
            (dict(seq=70000), "bad_seq"),
            (dict(seq=1.5), "bad_seq"),
        ]
        for over, reason in cases:
            if reason is None:
                continue                      # NaN cannot be written as JSON anyway
            with self.subTest(over=over):
                self.assertEqual(self.row_reason(**over), [reason])

    def test_teleport_costs_one_row(self):
        pts = [point(i) for i in range(30)]
        pts[15] = point(15, lat=56.75)        # 100 km away, 10 s after its neighbour
        r = run(pts)
        self.assertIsNone(r.batch_error)
        self.assertEqual(r.rejected["speed"], 1)
        self.assertEqual(len(r.rows), 29)

    def test_duplicates_are_not_failures(self):
        pts = [point(i) for i in range(10)]
        r = run(pts + pts)                    # an overlapping export
        self.assertIsNone(r.batch_error)
        self.assertEqual(r.duplicates, 10)
        self.assertEqual(len(r.rows), 10)

    def test_mostly_invalid_sinks_batch(self):
        pts = [point(i) for i in range(10)] + [point(i, rssi=50) for i in range(10, 20)]
        self.assertEqual(run(pts).batch_error, "too_many_invalid")

    def test_hostile_values_are_rejections_not_crashes(self):
        huge = 10 ** 5000                     # float(huge) raises OverflowError
        for over in (dict(rssi=huge), dict(lat=huge), dict(time_ms=huge), dict(seq=huge),
                     dict(rssi=[1]), dict(responder_id={"a": 1}), dict(session=["x"]),
                     dict(time=["x"], time_ms=None), dict(snr=True), dict(accuracy_m="1e999")):
            with self.subTest(over=over):
                row, why = V.normalise_row(point(1, **over), NOW, V.Limits())
                self.assertIsNone(row)
                self.assertTrue(why)

    def test_full_size_upload_is_fast(self):
        pts = [point(i, seq=i % 65536, time_ms=NOW - 3 * 86400_000 + i * 10_000,
                     lat=55.0 + i * 1e-5) for i in range(20000)]
        t0 = time.time()
        r = V.validate_batch(V.parse_export("big.geojson", geojson(pts)), NOW)
        self.assertLess(time.time() - t0, 3.0)
        self.assertGreater(len(r.rows), 19000)

    def test_empty_and_all_bad(self):
        self.assertEqual(run([]).batch_error, "no_valid_rows")
        self.assertEqual(run([point(0, rssi=50)]).batch_error, "no_valid_rows")

    def test_synthetic_constant_signal(self):
        pts = [point(i, rssi=-90, snr=5.0) for i in range(60)]
        self.assertEqual(run(pts).batch_error, "synthetic")

    def test_span_too_long(self):
        lim = V.Limits(max_span_days=10)
        r = V.validate_batch(V.parse_export("x.geojson", geojson(
            [point(0, time_ms=NOW - 20 * 86400_000), point(1, time_ms=NOW - 1000)])), NOW, lim)
        self.assertEqual(r.batch_error, "span_too_long")

    def test_formats_agree(self):
        pts = [point(i) for i in range(5)]
        csv_text = "time_utc,time_ms,lat,lon,accuracy_m,provider,session,seq,responder_id,replied," \
                   "ul_rssi_dbm,ul_snr_db,tx_power_dbm,rtt_ms,dl_rssi_dbm,dl_snr_db\r\n"
        for p in pts:
            csv_text += ",".join(str(x) for x in (
                "", p["time_ms"], p["lat"], p["lon"], p["accuracy_m"], "gps", p["session"], p["seq"],
                p["responder_id"], "true", p["rssi"], p["snr"], 14, 900, p["dl_rssi"], p["dl_snr"])) + "\r\n"
        gpx = ('<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" '
               'xmlns="http://www.topografix.com/GPX/1/1" xmlns:wd="https://rns.moscow/wardrive/1">\n'
               '<trk><name>Wardrive session a1b2c3d4</name><trkseg>\n')
        for p in pts:
            iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p["time_ms"] / 1000))
            gpx += (f'<trkpt lat="{p["lat"]}" lon="{p["lon"]}"><time>{iso}</time><extensions>'
                    f'<wd:seq>{p["seq"]}</wd:seq><wd:accuracy_m>5.0</wd:accuracy_m>'
                    f'<wd:replied>true</wd:replied><wd:responder>0badf00d</wd:responder>'
                    f'<wd:ul_rssi_dbm>{p["rssi"]}</wd:ul_rssi_dbm><wd:ul_snr_db>{p["snr"]}</wd:ul_snr_db>'
                    f'</extensions></trkpt>\n')
        gpx += "</trkseg></trk></gpx>\n"
        for name, body in (("a.csv", csv_text), ("a.gpx", gpx)):
            with self.subTest(name=name):
                r = V.validate_batch(V.parse_export(name, body.encode()), NOW)
                self.assertIsNone(r.batch_error, r.rejected)
                self.assertEqual(len(r.rows), 5)
                self.assertEqual({x["session"] for x in r.rows}, {"a1b2c3d4"})

    def test_parse_refusals(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><gpx>&a;</gpx>'
        for name, data, why in [
            ("a.gpx", bomb, "xml_dtd_forbidden"),
            ("a.gpx", b"<gpx><trk>", "bad_xml"),
            ("a.json", b"{not json", "bad_json"),
            ("a.json", b"[" * 100000, "bad_json"),
            ("a.json", b"\xff\xfe\x00bad", "not_utf8"),
            ("a.json", b'"just a string"', "bad_geojson"),
        ]:
            with self.subTest(why=why):
                with self.assertRaises(V.ParseError) as cm:
                    V.parse_export(name, data)
                self.assertEqual(cm.exception.args[0], why)
        with self.assertRaises(V.ParseError) as cm:
            V.parse_export("a.geojson", geojson([point(i) for i in range(30)]),
                           V.Limits(max_rows=10))
        self.assertEqual(cm.exception.args[0], "too_many_rows")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OverlayStore(os.path.join(self.dir.name, "overlay.db"))
        cid, tok = self.store.create_contributor("alice")
        self.alice = self.store.authenticate(tok)
        self.token = tok

    def tearDown(self):
        self.dir.cleanup()

    def upload(self, who, pts, sha=None):
        res = V.validate_batch(V.parse_export("x.geojson", geojson(pts)), NOW)
        return self.store.add_upload(who, sha or hash_secret(json.dumps(pts)), res)

    def test_token_is_stored_hashed(self):
        import sqlite3
        db = sqlite3.connect(self.store.path)
        raw = db.execute("SELECT token_hash FROM contributors").fetchone()[0]
        db.close()
        self.assertNotIn(self.token, raw)
        self.assertEqual(raw, hash_secret(self.token))
        self.assertIsNone(self.store.authenticate("wd_wrong"))
        self.assertIsNone(self.store.authenticate(""))

    def test_publish_and_public_shape(self):
        out = self.upload(self.alice, [point(i) for i in range(20)])
        self.assertEqual(out["accepted"], 20)
        fc = self.store.public_geojson(self.store.published())
        self.assertEqual(len(fc["features"]), 20)
        f = fc["features"][0]
        self.assertEqual(set(f["properties"]), {"ts", "replied", "rssi", "snr", "dl_rssi", "dl_snr"})
        self.assertEqual(f["properties"]["ts"] % 3600, 0)
        self.assertLessEqual(len(str(f["geometry"]["coordinates"][0]).split(".")[1]), 5)
        text = json.dumps(fc)
        for secret in ("a1b2c3d4", "0badf00d", "alice", "session", "responder"):
            self.assertNotIn(secret, text)

    def test_rejected_batch_publishes_nothing_but_counts(self):
        out = self.upload(self.alice, [point(i, rssi=50) for i in range(5)])
        self.assertEqual(out["accepted"], 0)
        self.assertEqual(out["error"], "no_valid_rows")
        self.assertEqual(self.store.published(), [])
        self.assertEqual(self.store.list_batches()[0]["status"], "rejected")

    def test_repeat_file_and_overlap(self):
        pts = [point(i) for i in range(20)]
        self.upload(self.alice, pts, sha="aaa")
        with self.assertRaises(StoreError) as cm:
            self.upload(self.alice, pts, sha="aaa")
        self.assertEqual(cm.exception.args[0], "duplicate_file")
        out = self.upload(self.alice, pts + [point(i) for i in range(20, 25)], sha="bbb")
        self.assertEqual(out["accepted"], 5)           # the first 20 were already there
        self.assertEqual(out["duplicates"], 20)
        again = self.upload(self.alice, pts, sha="ccc")
        self.assertEqual(again["error"], "all_duplicates")

    def test_quotas(self):
        self.store.set_limits(self.alice["id"], max_batches_day=2, max_rows_day=25)
        who = self.store.authenticate(self.token)
        self.upload(who, [point(i) for i in range(20)], sha="1")
        with self.assertRaises(StoreError) as cm:      # 20 + 20 > 25 rows
            self.upload(who, [point(i + 100) for i in range(20)], sha="2")
        self.assertEqual(cm.exception.args[0], "quota_rows")
        self.upload(who, [point(i + 300) for i in range(3)], sha="3")
        with self.assertRaises(StoreError) as cm:
            self.store.check_batch_quota(who)
        self.assertEqual(cm.exception.args[0], "quota_batches")

    def test_quota_status(self):
        st = self.store.quota_status(self.alice)
        self.assertEqual(st, {"name": "alice", "rows_left_today": 20000, "batches_left_today": 10,
                              "invites_left": 3})
        self.upload(self.alice, [point(i) for i in range(20)], sha="q1")
        self.store.create_invite(self.alice["id"])
        st = self.store.quota_status(self.alice)
        self.assertEqual((st["rows_left_today"], st["batches_left_today"], st["invites_left"]), (19980, 9, 2))
        self.store.set_limits(self.alice["id"], max_rows_day=5)
        self.assertEqual(self.store.quota_status(self.store.authenticate(self.token))["rows_left_today"], 0)

    def test_total_ceiling(self):
        small = OverlayStore(os.path.join(self.dir.name, "small.db"), max_total_rows=10)
        _, tok = small.create_contributor("bob")
        with self.assertRaises(StoreError) as cm:
            res = V.validate_batch(V.parse_export("x.geojson", geojson([point(i) for i in range(20)])), NOW)
            small.add_upload(small.authenticate(tok), "s", res)
        self.assertEqual(cm.exception.args[0], "overlay_full")

    def test_invites_and_cascade_ban(self):
        code = self.store.create_invite(self.alice["id"])
        _, bob_tok = self.store.redeem_invite(code, "bob\x1b[31m")
        bob = self.store.authenticate(bob_tok)
        self.assertNotIn("\x1b", bob["name"])          # no terminal escapes into the operator's CLI
        self.assertEqual(bob["invited_by"], self.alice["id"])
        with self.assertRaises(StoreError):
            self.store.redeem_invite(code, "mallory")  # single use
        with self.assertRaises(StoreError):
            self.store.redeem_invite("inv_nonsense", "x")
        for _ in range(2):
            self.store.create_invite(self.alice["id"])
        with self.assertRaises(StoreError) as cm:      # max_invites is 3
            self.store.create_invite(self.alice["id"])
        self.assertEqual(cm.exception.args[0], "invite_quota")

        self.upload(self.alice, [point(i) for i in range(10)], sha="a")
        self.upload(bob, [point(i, seq=i + 50) for i in range(10)], sha="b")
        self.assertEqual(len(self.store.published()), 20)

        removed = self.store.ban(bob["id"])            # no cascade: only bob's rows go
        self.assertEqual(sum(removed.values()), 10)
        self.assertEqual(len(self.store.published()), 10)
        self.assertIsNone(self.store.authenticate(bob_tok))

        _, carol_tok = self.store.redeem_invite(self.store.create_invite(None), "carol")
        carol = self.store.authenticate(carol_tok)
        dave_code = self.store.create_invite(carol["id"])
        _, dave_tok = self.store.redeem_invite(dave_code, "dave")
        self.upload(self.store.authenticate(dave_tok), [point(i, seq=i + 80) for i in range(10)], sha="d")
        self.store.ban(carol["id"], cascade=True)
        self.assertIsNone(self.store.authenticate(dave_tok))
        self.assertEqual(len(self.store.published()), 10)   # only alice's remain

    def test_banned_inviter_voids_invite(self):
        code = self.store.create_invite(self.alice["id"])
        self.store.ban(self.alice["id"])
        with self.assertRaises(StoreError):
            self.store.redeem_invite(code, "late")

    def test_remove_batch_and_bbox(self):
        out = self.upload(self.alice, [point(i) for i in range(10)])
        self.assertEqual(len(self.store.published(bbox=(37.0, 55.0, 38.0, 56.0))), 10)
        self.assertEqual(self.store.published(bbox=(0, 0, 1, 1)), [])
        self.assertEqual(self.store.remove_batch(out["batch"]), 10)
        self.assertEqual(self.store.published(), [])
        self.assertEqual(self.store.list_batches()[0]["status"], "removed")


class AdminWebTests(unittest.TestCase):
    """overlay_admin_web.py: read-only, loopback, escapes everything a contributor controls."""

    @classmethod
    def setUpClass(cls):
        import http.client
        import sqlite3
        import threading
        import overlay_admin_web as W
        cls.http, cls.sqlite3 = http.client, sqlite3
        cls.dir = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.dir.name, "overlay.db")
        store = OverlayStore(cls.path)
        cls.evil = '<img src=x onerror=alert(1)>"\'&'
        _, tok = store.create_contributor(cls.evil)
        cls.who = store.authenticate(tok)
        cls.ok = store.add_upload(cls.who, "s1", V.validate_batch(
            V.parse_export("a.geojson", geojson([point(i) for i in range(40)])), NOW))
        cls.bad = store.add_upload(cls.who, "s2", V.validate_batch(
            V.parse_export("b.geojson", geojson([point(i, rssi=50) for i in range(5)])), NOW))
        _, tok2 = store.redeem_invite(store.create_invite(cls.who["id"]), "bob")
        cls.gone = store.add_upload(store.authenticate(tok2), "s3", V.validate_batch(
            V.parse_export("c.geojson", geojson([point(i, seq=900 + i) for i in range(10)])), NOW))
        store.remove_batch(cls.gone["batch"])
        cls.srv = W.make_server(cls.path, 0)
        cls.port = cls.srv.server_port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.dir.cleanup()

    def get(self, path, method="GET", host=None):
        c = self.http.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, headers={"Host": f"127.0.0.1:{self.port}" if host is None else host})
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return r.status, body, r

    def test_pages_render(self):
        s, body, r = self.get("/")
        self.assertEqual(s, 200)
        for want in ("entries on the map", "Contributors", "Recent sessions", "Invites", "Activity", "no_valid_rows"):
            self.assertIn(want, body)
        self.assertIn("rssi", self.get(f"/batch/{self.ok['batch']}")[1].lower())
        self.assertIn("<svg", self.get(f"/batch/{self.ok['batch']}")[1])
        self.assertEqual(self.get(f"/contributor/{self.who['id']}")[0], 200)
        self.assertIn("it was removed", self.get(f"/batch/{self.gone['batch']}")[1])
        self.assertIn("rejected", self.get(f"/batch/{self.bad['batch']}")[1])

    def test_contributor_text_is_escaped_everywhere(self):
        for path in ("/", f"/contributor/{self.who['id']}", f"/batch/{self.ok['batch']}"):
            body = self.get(path)[1]
            self.assertNotIn("<img src=x", body, path)
            self.assertIn("&lt;img src=x onerror=alert(1)&gt;", body, path)

    def test_headers(self):
        _, _, r = self.get("/")
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))
        self.assertNotIn("script-src", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("Cache-Control"), "no-store, private")
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")

    def test_only_get_and_known_pages(self):
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.get("/", m)[0], 405, m)
        for p in ("/nope", "/batch/9999", "/contributor/9999", "/batch/abc", "/batch/1/2", "/../etc/passwd",
                  "/batch/1;DROP%20TABLE%20samples"):
            self.assertEqual(self.get(p)[0], 404, p)

    def test_dns_rebinding_host_is_refused(self):
        for h in ("evil.example", f"evil.example:{self.port}", "", f"127.0.0.1:{self.port + 1}"):
            self.assertEqual(self.get("/", host=h)[0], 421, h)

    def test_binds_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_database_is_opened_read_only(self):
        import overlay_admin_web as W
        view = W.View(self.path)
        for sql in ("DELETE FROM samples", "UPDATE contributors SET status='active'",
                    "INSERT INTO invites (code_hash, created_at, expires_at) VALUES ('x',0,0)", "DROP TABLE samples"):
            with self.assertRaises(self.sqlite3.OperationalError, msg=sql):
                view.q(sql)
        db = self.sqlite3.connect(self.path)
        n = db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        db.close()
        self.assertEqual(n, 40)

    def test_missing_db_does_not_create_one(self):
        import overlay_admin_web as W
        ghost = os.path.join(self.dir.name, "ghost.db")
        with self.assertRaises(self.sqlite3.OperationalError):
            W.View(ghost).q("SELECT 1 FROM samples")
        self.assertFalse(os.path.exists(ghost))


class StoreAdminOpsTests(unittest.TestCase):
    """The store operations behind the admin page."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OverlayStore(os.path.join(self.dir.name, "o.db"))

    def tearDown(self):
        self.dir.cleanup()

    def test_rotate_token_replaces_the_old_one(self):
        cid, old = self.store.create_contributor("ann")
        new = self.store.rotate_token(cid)
        self.assertNotEqual(old, new)
        self.assertIsNone(self.store.authenticate(old))
        self.assertEqual(self.store.authenticate(new)["id"], cid)

    def test_rotate_refuses_banned_and_unknown(self):
        cid, _ = self.store.create_contributor("ann")
        self.store.ban(cid)
        with self.assertRaises(StoreError) as e:
            self.store.rotate_token(cid)
        self.assertEqual(e.exception.args[0], "banned")
        with self.assertRaises(StoreError) as e:
            self.store.rotate_token(999)
        self.assertEqual(e.exception.args[0], "no_such_contributor")

    def test_reinstate_only_for_banned(self):
        cid, tok = self.store.create_contributor("ann")
        with self.assertRaises(StoreError) as e:
            self.store.reinstate(cid)
        self.assertEqual(e.exception.args[0], "not_banned")
        self.store.ban(cid)
        self.assertIsNone(self.store.authenticate(tok))
        self.store.reinstate(cid)
        self.assertEqual(self.store.authenticate(tok)["id"], cid)

    def test_rename_validates_like_signup(self):
        cid, _ = self.store.create_contributor("ann")
        self.assertEqual(self.store.rename(cid, "  Bea  "), "Bea")
        self.assertEqual(self.store.get_contributor(cid)["name"], "Bea")
        for bad in ("", "   ", "\n\t"):
            with self.assertRaises(StoreError):
                self.store.rename(cid, bad)
        with self.assertRaises(StoreError):
            self.store.rename(999, "x")

    def test_void_invite(self):
        code = self.store.create_invite(None)
        self.store.void_invite(hash_secret(code))
        with self.assertRaises(StoreError) as e:
            self.store.redeem_invite(code, "late")
        self.assertEqual(e.exception.args[0], "bad_invite")
        used = self.store.create_invite(None)
        self.store.redeem_invite(used, "bob")
        with self.assertRaises(StoreError) as e:
            self.store.void_invite(hash_secret(used))
        self.assertEqual(e.exception.args[0], "invite_used")
        with self.assertRaises(StoreError) as e:
            self.store.void_invite("0" * 64)
        self.assertEqual(e.exception.args[0], "no_such_invite")

    def test_limits_are_range_checked(self):
        cid, _ = self.store.create_contributor("ann")
        self.store.set_limits(cid, max_rows_day=5, max_batches_day=0, max_invites=1)
        c = self.store.get_contributor(cid)
        self.assertEqual((c["max_rows_day"], c["max_batches_day"], c["max_invites"]), (5, 0, 1))
        for bad in (-1, 10_000_001):
            with self.assertRaises(StoreError) as e:
                self.store.set_limits(cid, max_rows_day=bad)
            self.assertEqual(e.exception.args[0], "bad_limit")
        self.assertEqual(self.store.get_contributor(cid)["max_rows_day"], 5)
        with self.assertRaises(StoreError):
            self.store.set_limits(999, max_rows_day=1)

    def test_admin_log_round_trip_newest_first(self):
        self.store.log_admin("nickie", "ban", "contributor 3", "8 entries")
        self.store.log_admin("", "invite", "operator invite")
        rows = self.store.list_admin_log()
        self.assertEqual([r["action"] for r in rows], ["invite", "ban"])
        self.assertEqual(rows[0]["actor"], "-")
        self.assertEqual(rows[1]["actor"], "nickie")

    def test_a_database_from_before_the_log_gains_the_table(self):
        import sqlite3
        path = os.path.join(self.dir.name, "old.db")
        OverlayStore(path)
        c = sqlite3.connect(path)
        c.execute("DROP TABLE admin_log")
        c.commit()
        c.close()
        self.assertEqual(OverlayStore(path).list_admin_log(), [])


class AdminWebWriteTests(unittest.TestCase):
    """overlay_admin_web.py with a store: every action, and the checks around a POST."""

    def setUp(self):
        import http.client
        import threading
        import overlay_admin_web as W
        self.http, self.W = http.client, W
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "overlay.db")
        self.store = OverlayStore(self.path)
        self.evil = '<img src=x onerror=alert(1)>"\'&'
        self.ann_id, self.ann_tok = self.store.create_contributor(self.evil)
        self.ann = self.store.authenticate(self.ann_tok)
        self.up = self.store.add_upload(self.ann, "s1", V.validate_batch(
            V.parse_export("a.geojson", geojson([point(i) for i in range(40)])), NOW))
        self.bob_id, self.bob_tok = self.store.redeem_invite(self.store.create_invite(self.ann_id), "bob")
        self.bob_up = self.store.add_upload(self.store.authenticate(self.bob_tok), "s2", V.validate_batch(
            V.parse_export("b.geojson", geojson([point(i, seq=500 + i) for i in range(12)])), NOW))
        self.srv = W.make_server(self.path, 0, store=self.store)
        self.port = self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.dir.cleanup()

    host = property(lambda self: f"127.0.0.1:{self.port}")

    def req(self, method, path, body=None, headers=None, ctype="application/x-www-form-urlencoded"):
        h = {"Host": self.host}
        if body is not None:
            h["Content-Type"] = ctype
        h.update(headers or {})
        c = self.http.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode()
        c.close()
        return r.status, data, r

    def csrf(self, headers=None):
        m = re.search(r'name="csrf" value="([0-9a-f]{64})"', self.req("GET", "/", headers=headers)[1])
        self.assertTrue(m, "the overview should carry a form with a CSRF token")
        return m.group(1)

    def post(self, action, fields=None, token=True, headers=None, user_headers=None):
        from urllib.parse import urlencode
        f = dict(fields or {})
        if token is True:
            f["csrf"] = self.csrf(user_headers)
        elif token:
            f["csrf"] = token
        return self.req("POST", f"/do/{action}", urlencode(f), headers)

    def where(self, r):
        return r.getheader("Location")

    def contributor_status(self, cid):
        return self.store.get_contributor(cid)["status"]

    # -- what is drawn --

    def test_forms_are_drawn_when_writable_and_the_header_says_so(self):
        body = self.req("GET", "/")[1]
        for want in ("Mint an invite", "Add contributor", "Activity"):
            self.assertIn(want, body)
        self.assertNotIn("read-only", body)
        c = self.req("GET", f"/contributor/{self.ann_id}")[1]
        for want in ("Rename", "Save limits", "Rotate token", "Ban…", "Ban with everyone they invited…"):
            self.assertIn(want, c)
        self.assertIn("Remove this session…", self.req("GET", f"/batch/{self.up['batch']}")[1])

    def test_the_contributors_table_uses_the_full_page_width(self):
        css = self.req("GET", "/")[1]
        main_rule = re.search(r"main \{([^}]*)\}", css).group(1)
        self.assertNotIn("max-width", main_rule)

    def test_terms_match_the_app(self):
        body = self.req("GET", "/")[1]
        for want in ("shared sessions", "Recent sessions", "entries on the map", "sessions / day", "last session"):
            self.assertIn(want, body)
        for old in ("upload", "Upload", "samples", "published"):
            self.assertNotIn(old, body, old)

    # -- invite / add --

    def test_mint_invite_shows_the_code_once_and_it_works(self):
        s, body, r = self.post("invite")
        self.assertEqual(s, 200)
        self.assertEqual(r.getheader("Cache-Control"), "no-store, private")
        code = re.search(r"inv_[A-Za-z0-9_-]+", body).group(0)
        cid, _ = self.store.redeem_invite(code, "carol")
        self.assertEqual(self.store.get_contributor(cid)["invited_by"], None)
        self.assertNotIn(code, json.dumps(self.store.list_admin_log()))      # the log never holds the secret
        self.assertEqual(self.store.list_admin_log()[0]["action"], "invite")

    def test_add_contributor_shows_a_working_token(self):
        s, body, _ = self.post("add", {"name": "  dave  "})
        self.assertEqual(s, 200)
        tok = re.search(r"wd_[A-Za-z0-9_-]+", body).group(0)
        self.assertEqual(self.store.authenticate(tok)["name"], "dave")
        self.assertNotIn(tok, json.dumps(self.store.list_admin_log()))

    def test_add_with_a_bad_name_is_refused(self):
        s, body, _ = self.post("add", {"name": "   "})
        self.assertEqual(s, 400)
        self.assertIn("not valid", body)

    # -- rename / limits --

    def test_rename_redirects_with_a_fixed_banner(self):
        s, _, r = self.post("rename", {"id": self.bob_id, "name": "Robert"})
        self.assertEqual(s, 303)
        self.assertEqual(self.where(r), f"/contributor/{self.bob_id}?msg=renamed")
        self.assertEqual(self.store.get_contributor(self.bob_id)["name"], "Robert")
        self.assertIn("Renamed.", self.req("GET", self.where(r))[1])

    def test_limits_saved_and_bad_values_refused(self):
        s, _, r = self.post("limits", {"id": self.bob_id, "rows": "100", "batches": "2", "invites": "0"})
        self.assertEqual(s, 303)
        c = self.store.get_contributor(self.bob_id)
        self.assertEqual((c["max_rows_day"], c["max_batches_day"], c["max_invites"]), (100, 2, 0))
        for bad in ({"rows": "-1"}, {"rows": "abc"}, {"rows": "99999999999"}, {"rows": "10000001"}, {"rows": ""}):
            f = {"id": self.bob_id, "rows": "5", "batches": "5", "invites": "5"}
            f.update(bad)
            self.assertEqual(self.post("limits", f)[0], 400, bad)
        self.assertEqual(self.store.get_contributor(self.bob_id)["max_rows_day"], 100)

    # -- rotate --

    def test_rotate_needs_its_confirmation_page(self):
        self.assertEqual(self.post("rotate", {"id": self.bob_id})[0], 400)            # no confirm=1
        self.assertEqual(self.store.authenticate(self.bob_tok)["id"], self.bob_id)    # unchanged
        s, page_, _ = self.req("GET", f"/confirm/rotate?id={self.bob_id}")
        self.assertEqual(s, 200)
        self.assertIn("stops working immediately", page_)
        self.assertEqual(self.store.authenticate(self.bob_tok)["id"], self.bob_id)    # a GET changes nothing
        s, body, _ = self.post("rotate", {"id": self.bob_id, "confirm": "1"})
        self.assertEqual(s, 200)
        new = re.search(r"wd_[A-Za-z0-9_-]+", body).group(0)
        self.assertIsNone(self.store.authenticate(self.bob_tok))
        self.assertEqual(self.store.authenticate(new)["id"], self.bob_id)
        self.assertNotIn(new, json.dumps(self.store.list_admin_log()))

    # -- ban / reinstate --

    def test_ban_confirmation_lists_exactly_what_goes(self):
        s, body, _ = self.req("GET", f"/confirm/ban?id={self.ann_id}")
        self.assertIn("(40 entries in all)", body)
        self.assertIn("only them", body)
        s, body, _ = self.req("GET", f"/confirm/ban?id={self.ann_id}&cascade=1")
        self.assertIn("(52 entries in all)", body)
        self.assertIn("bob", body)
        self.assertEqual(self.contributor_status(self.ann_id), "active")              # still nothing done

    def test_ban_without_cascade_then_reinstate(self):
        self.assertEqual(self.post("ban", {"id": self.ann_id})[0], 400)               # not from the confirm page
        s, _, r = self.post("ban", {"id": self.ann_id, "cascade": "", "confirm": "1"})
        self.assertEqual((s, self.where(r)), (303, f"/contributor/{self.ann_id}?msg=banned&n=40"))
        self.assertEqual(self.contributor_status(self.ann_id), "banned")
        self.assertEqual(self.contributor_status(self.bob_id), "active")
        self.assertEqual(len(self.store.published()), 12)                              # bob's entries stay
        self.assertIn("Banned. 40 entries deleted.", self.req("GET", self.where(r))[1])
        self.assertIn("Reinstate", self.req("GET", f"/contributor/{self.ann_id}")[1])
        s, _, r = self.post("reinstate", {"id": self.ann_id})
        self.assertEqual(s, 303)
        self.assertEqual(self.contributor_status(self.ann_id), "active")
        self.assertEqual(len(self.store.published()), 12)                              # nothing came back

    def test_ban_with_cascade_takes_the_invitee_too(self):
        s, _, r = self.post("ban", {"id": self.ann_id, "cascade": "1", "confirm": "1"})
        self.assertEqual(self.where(r), f"/contributor/{self.ann_id}?msg=banned&n=52")
        self.assertEqual(self.contributor_status(self.bob_id), "banned")
        self.assertEqual(self.store.published(), [])

    # -- remove a session --

    def test_remove_session(self):
        bid = self.bob_up["batch"]
        s, body, _ = self.req("GET", f"/confirm/remove-batch?id={bid}")
        self.assertIn("Its 12 entries are deleted", body)
        self.assertEqual(self.post("remove-batch", {"id": bid})[0], 400)
        s, _, r = self.post("remove-batch", {"id": bid, "confirm": "1"})
        self.assertEqual((s, self.where(r)), (303, f"/batch/{bid}?msg=removed&n=12"))
        self.assertEqual(len(self.store.published()), 40)
        self.assertIn("it was removed", self.req("GET", self.where(r))[1])
        # nothing left to remove: the confirmation page says so
        self.assertIn("nothing is on the map", self.req("GET", f"/confirm/remove-batch?id={bid}")[1])

    # -- void an invite --

    def test_void_invite(self):
        code = self.store.create_invite(None)
        h = hash_secret(code)
        self.assertIn(h, self.req("GET", "/")[1])
        s, _, r = self.post("void-invite", {"code_hash": h})
        self.assertEqual((s, self.where(r)), (303, "/?msg=voided"))
        with self.assertRaises(StoreError):
            self.store.redeem_invite(code, "late")
        used = self.store.create_invite(None)
        self.store.redeem_invite(used, "eve")
        self.assertEqual(self.post("void-invite", {"code_hash": hash_secret(used)})[0], 409)
        self.assertEqual(self.post("void-invite", {"code_hash": "zz"})[0], 404)

    # -- the checks around every POST --

    def test_csrf_token_is_required_and_checked(self):
        self.assertEqual(self.post("invite", token=False)[0], 403)
        self.assertEqual(self.post("invite", token="0" * 64)[0], 403)
        self.assertEqual(self.post("invite", token="short")[0], 403)
        self.assertEqual(self.store.list_admin_log(), [])

    def test_a_token_for_another_user_is_refused(self):
        W = self.W
        srv = W.make_server(self.path, 0, base="/overlay-admin", public_hosts=("map.example.org",), store=self.store)
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def go(method, path, user, body=None):
                c = self.http.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
                h = {"Host": "map.example.org", "X-Remote-User": user}
                if body is not None:
                    h["Content-Type"] = "application/x-www-form-urlencoded"
                c.request(method, path, body=body, headers=h)
                r = c.getresponse()
                return r.status, r.read().decode()
            tok = re.search(r'name="csrf" value="([0-9a-f]{64})"', go("GET", "/overlay-admin/", "alice")[1]).group(1)
            self.assertEqual(go("POST", "/overlay-admin/do/invite", "bob", f"csrf={tok}")[0], 403)
            self.assertEqual(go("POST", "/overlay-admin/do/invite", "alice", f"csrf={tok}")[0], 200)
            self.assertEqual(self.store.list_admin_log()[0]["actor"], "alice")
            # public mode with no proxy header: refused before anything else
            c = self.http.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
            c.request("POST", "/overlay-admin/do/invite", body=f"csrf={tok}",
                      headers={"Host": "map.example.org", "Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(c.getresponse().status, 403)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_cross_site_posts_are_refused(self):
        tok = self.csrf()
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            self.assertEqual(self.post("invite", token=tok, headers=headers)[0], 403, headers)
        self.assertEqual(self.post("invite", token=tok, headers={"Origin": f"http://{self.host}"})[0], 200)
        self.assertEqual(self.post("invite", token=tok, headers={"Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_form_shape_is_enforced(self):
        tok = self.csrf()
        self.assertEqual(self.req("POST", "/do/invite", f"csrf={tok}", ctype="application/json")[0], 415)
        self.assertEqual(self.req("POST", "/do/invite", "x" * 9000)[0], 413)
        self.assertEqual(self.req("POST", "/do/nope", f"csrf={tok}")[0], 404)
        self.assertEqual(self.req("POST", "/", f"csrf={tok}")[0], 405)
        self.assertEqual(self.req("POST", "/do/invite/extra", f"csrf={tok}")[0], 405)
        self.assertEqual(self.req("PUT", "/do/invite", f"csrf={tok}")[0], 405)
        self.assertEqual(self.req("GET", "/do/invite")[0], 404)         # no action is reachable by GET
        for p in ("/confirm/invite?id=1", "/confirm/ban", "/confirm/ban?id=x", "/confirm/ban?id=9999",
                  "/confirm/remove-batch?id=9999"):
            self.assertEqual(self.req("GET", p)[0], 404, p)

    def test_wrong_host_is_refused_for_posts_too(self):
        self.assertEqual(self.req("POST", "/do/invite", "csrf=x", headers={"Host": "evil.example"})[0], 421)

    def test_unknown_ids_are_reported_not_crashed(self):
        for action, f in (("rename", {"id": "999", "name": "x"}), ("rename", {"id": "abc", "name": "x"}),
                          ("limits", {"id": "999", "rows": "1", "batches": "1", "invites": "1"}),
                          ("reinstate", {"id": "999"})):
            self.assertIn(self.post(action, f)[0], (400, 404), (action, f))
        self.assertEqual(self.post("ban", {"id": "999", "confirm": "1"})[0], 404)
        self.assertEqual(self.post("remove-batch", {"id": "999", "confirm": "1"})[0], 404)

    # -- activity log --

    def test_every_action_lands_in_the_log_and_on_the_page_escaped(self):
        self.post("rename", {"id": self.bob_id, "name": self.evil})
        self.post("limits", {"id": self.bob_id, "rows": "5", "batches": "5", "invites": "5"})
        rows = self.store.list_admin_log()
        self.assertEqual([r["action"] for r in rows], ["limits", "rename"])
        self.assertEqual(rows[0]["actor"], "local")                      # no proxy header in this mode
        body = self.req("GET", "/")[1]
        self.assertIn("contributor %d" % self.bob_id, body)
        self.assertNotIn("<img src=x", body)

    def test_the_hostile_name_is_escaped_on_forms_and_confirm_pages(self):
        for path in (f"/contributor/{self.ann_id}", f"/confirm/ban?id={self.ann_id}",
                     f"/confirm/ban?id={self.ann_id}&cascade=1", f"/confirm/rotate?id={self.ann_id}"):
            body = self.req("GET", path)[1]
            self.assertNotIn("<img src=x", body, path)
            self.assertIn("&lt;img src=x onerror=alert(1)&gt;", body, path)

    def test_the_policy_allows_form_posts_but_still_no_script(self):
        csp = self.req("GET", "/")[2].getheader("Content-Security-Policy")
        self.assertIn("form-action 'self'", csp)
        self.assertNotIn("script-src", csp)
        self.assertIn("default-src 'none'", csp)

    def test_read_only_mode_refuses_everything_and_draws_no_forms(self):
        import threading
        srv = self.W.make_server(self.path, 0)                           # no store
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def go(method, path, body=None):
                c = self.http.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
                h = {"Host": f"127.0.0.1:{srv.server_port}"}
                if body:
                    h["Content-Type"] = "application/x-www-form-urlencoded"
                c.request(method, path, body=body, headers=h)
                r = c.getresponse()
                return r.status, r.read().decode()
            s, body = go("GET", "/")
            self.assertIn("read-only", body)
            self.assertNotIn("<form", body)
            self.assertEqual(go("POST", "/do/invite", "csrf=x")[0], 405)
            self.assertEqual(go("GET", f"/confirm/ban?id={self.ann_id}")[0], 404)
            self.assertEqual(self.store.list_admin_log(), [])
        finally:
            srv.shutdown()
            srv.server_close()


class AdminWebPublicTests(unittest.TestCase):
    """overlay_admin_web.py behind nginx: path prefix, public host name, fail-closed auth header."""
    BASE, HOST = "/overlay-admin", "map.example.org"

    @classmethod
    def setUpClass(cls):
        import http.client
        import threading
        import overlay_admin_web as W
        cls.http = http.client
        cls.dir = tempfile.TemporaryDirectory()
        path = os.path.join(cls.dir.name, "overlay.db")
        store = OverlayStore(path)
        _, tok = store.create_contributor("alice")
        cls.ok = store.add_upload(store.authenticate(tok), "p1", V.validate_batch(
            V.parse_export("a.geojson", geojson([point(i) for i in range(20)])), NOW))
        cls.srv = W.make_server(path, 0, base=cls.BASE + "/", public_hosts=[cls.HOST.upper()])
        cls.port = cls.srv.server_port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.dir.cleanup()

    def get(self, path, user="nickie", host=None, method="GET"):
        c = self.http.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": self.HOST if host is None else host}
        if user is not None:
            h["X-Remote-User"] = user
        c.request(method, path, headers=h)
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return r.status, body, r

    def test_served_below_the_prefix_with_working_links(self):
        s, body, _ = self.get(self.BASE + "/")
        self.assertEqual(s, 200)
        self.assertIn(f'href="{self.BASE}/contributor/1"', body)
        self.assertIn(f'href="{self.BASE}/batch/{self.ok["batch"]}"', body)
        self.assertTrue(re.findall(r'href="([^"]*)"', body))
        for href in re.findall(r'href="([^"]*)"', body):       # every link stays below the prefix
            self.assertTrue(href.startswith(self.BASE + "/"), href)
        s, body, _ = self.get(f"{self.BASE}/batch/{self.ok['batch']}")
        self.assertEqual(s, 200)
        self.assertIn(f'href="{self.BASE}/"', body)           # breadcrumb
        for href in re.findall(r'href="([^"]*)"', body):
            self.assertTrue(href.startswith(self.BASE + "/"), href)
        self.assertEqual(self.get(f"{self.BASE}/contributor/1")[0], 200)

    def test_prefix_without_slash_redirects_and_outside_is_404(self):
        s, _, r = self.get(self.BASE)
        self.assertEqual((s, r.getheader("Location")), (302, self.BASE + "/"))
        for p in ("/", "/batch/1", "/overlay-adminx/", self.BASE + "x/batch/1", "/other" + self.BASE + "/"):
            self.assertEqual(self.get(p)[0], 404, p)
        self.assertEqual(self.get(self.BASE + "/nope")[0], 404)
        self.assertEqual(self.get(self.BASE + "/batch/1;junk")[0], 404)

    def test_fails_closed_without_the_proxy_user(self):
        for user in (None, "", "   "):
            s, body, _ = self.get(self.BASE + "/", user=user)
            self.assertEqual(s, 403, repr(user))
            self.assertIn("auth_basic", body)
            self.assertNotIn("alice", body)                  # nothing leaks in the refusal
        for p in (f"{self.BASE}/batch/1", f"{self.BASE}/contributor/1", "/", self.BASE):
            self.assertEqual(self.get(p, user=None)[0], 403, p)   # even paths that would 404/redirect

    def test_host_must_be_the_public_name_or_loopback(self):
        self.assertEqual(self.get(self.BASE + "/", host=self.HOST)[0], 200)
        self.assertEqual(self.get(self.BASE + "/", host=f"127.0.0.1:{self.port}")[0], 200)
        for h in ("evil.example", f"{self.HOST}.evil.example", f"x{self.HOST}", ""):
            self.assertEqual(self.get(self.BASE + "/", host=h)[0], 421, h)

    def test_signed_in_name_is_shown_escaped(self):
        s, body, _ = self.get(self.BASE + "/", user="<img src=x onerror=alert(1)>")
        self.assertEqual(s, 200)
        self.assertNotIn("<img src=x", body)
        self.assertIn("Signed in as <b>&lt;img src=x onerror=alert(1)&gt;</b>", body)

    def test_not_cacheable_or_indexable_and_still_get_only(self):
        _, _, r = self.get(self.BASE + "/")
        self.assertEqual(r.getheader("Cache-Control"), "no-store, private")
        self.assertEqual(r.getheader("X-Robots-Tag"), "noindex")
        self.assertEqual(self.get(self.BASE + "/", method="POST")[0], 405)

    def test_option_validation_and_loopback_only(self):
        import overlay_admin_web as W
        path = os.path.join(self.dir.name, "overlay.db")
        for base in ("overlay-admin", "/a b", "/a//b", "/../x", "/a?b", '/"x'):
            with self.assertRaises(ValueError, msg=base):
                W.make_server(path, 0, base=base)
        for host in ("evil.example/", "a b", "ex@mple.com", "http://x", "-x.com"):
            with self.assertRaises(ValueError, msg=host):
                W.make_server(path, 0, public_hosts=[host])
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_plain_mode_still_needs_no_header(self):
        import overlay_admin_web as W
        srv = W.make_server(os.path.join(self.dir.name, "overlay.db"), 0)
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = self.http.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
            c.request("GET", "/", headers={"Host": f"127.0.0.1:{srv.server_port}"})
            self.assertEqual(c.getresponse().status, 200)
            c.close()
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
