#!/usr/bin/env python3
"""Tests for halow_telemetry.py.   python3 map/test_halow.py"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import halow_telemetry as H

NOW = 1_794_180_000
REAL = {"id": 3328097428, "name": "Catz Node (Halow)", "fw": "2.3.0",
        "loc": "55.643070,37.527630", "freq": "864.5 MHz", "bw": "1 MHz",
        "uptime": 536345, "mcs": 0, "lxmf": "aa72da8f4cd0df1f1729b3e3ce17e52e",
        "tx_b": 53952752, "rx_b": 0, "tx_p": 409716, "rx_p": 0}


def parse(**over):
    d = dict(REAL)
    d.update(over)
    return H.parse_telemetry(json.dumps(d).encode(), NOW)


class Parse(unittest.TestCase):
    def test_real_payload(self):
        key, n = parse()
        self.assertEqual(key, REAL["lxmf"])
        self.assertEqual(n["name"], "Catz Node (Halow)")
        self.assertEqual((n["lat"], n["lon"]), (55.64307, 37.52763))
        self.assertEqual(n["type"], "HaLow")
        self.assertEqual(n["last_heard"], NOW)
        self.assertEqual((n["freq_str"], n["bw_str"], n["fw"]), ("864.5 MHz", "1 MHz", "2.3.0"))
        self.assertEqual((n["uptime"], n["mcs"], n["tx_p"], n["rx_p"]), (536345, 0, 409716, 0))

    def test_not_json_or_not_object(self):
        for p in (b"", b"nope", b"[1,2]", b"\xff\xfe", b'"str"', b"null"):
            self.assertIsNone(H.parse_telemetry(p, NOW), p)
        self.assertIsNone(H.parse_telemetry("str", NOW))

    def test_oversize(self):
        self.assertIsNone(parse(name="a" * 5000))

    def test_position_required_and_sane(self):
        for loc in (None, "", "0.000000,0.000000", "abc,def", "1,2,3", "91,0", "0,181",
                    "nan,1", "inf,1", 55.6, ["55", "37"]):
            self.assertIsNone(parse(loc=loc), loc)
        self.assertIsNone(H.parse_telemetry(json.dumps({"name": "x"}).encode(), NOW))

    def test_southern_western_hemisphere(self):
        _, n = parse(loc="-33.9, -70.6")
        self.assertEqual((n["lat"], n["lon"]), (-33.9, -70.6))

    def test_html_cannot_get_through(self):
        evil = '<img src=x onerror=alert(1)>"\'&'
        _, n = parse(name=evil + " ok", fw=evil, freq=evil, bw=evil)
        for field in ("name", "fw", "freq_str", "bw_str"):
            for ch in '<>"\'&':
                self.assertNotIn(ch, n[field], field)
        self.assertEqual((n["fw"], n["freq_str"], n["bw_str"]), ("", "", ""))
        self.assertIn("ok", n["name"])

    def test_cyrillic_name_kept(self):
        _, n = parse(name="Нода Тест-1")
        self.assertEqual(n["name"], "Нода Тест-1")

    def test_name_fallback_and_length(self):
        self.assertEqual(parse(name="<<>>")[1]["name"], "HaLow node")
        self.assertEqual(parse(name=None)[1]["name"], "HaLow node")
        self.assertEqual(len(parse(name="a" * 300)[1]["name"]), 64)

    def test_lxmf_validated(self):
        for bad in ("XYZ", "AA" * 16, "aa" * 15, 5, None, "aa72da8f4cd0df1f1729b3e3ce17e52e<"):
            self.assertEqual(parse(lxmf=bad)[1]["lxmf"], "", bad)

    def test_numbers_validated(self):
        _, n = parse(uptime="1", mcs=True, tx_p=-1, rx_p=1.5)
        self.assertIsNone(n["uptime"])
        self.assertIsNone(n["mcs"])
        self.assertNotIn("tx_p", n)
        _, n = parse(uptime=1e30, tx_p=10, rx_p=2.0)
        self.assertIsNone(n["uptime"])
        self.assertEqual((n["tx_p"], n["rx_p"]), (10, 2))

    def test_no_packet_counters(self):
        d = dict(REAL)
        del d["tx_p"], d["rx_p"]
        _, n = H.parse_telemetry(json.dumps(d).encode(), NOW)
        self.assertNotIn("tx_p", n)
        self.assertNotIn("rx_p", n)


class Keys(unittest.TestCase):
    def test_lxmf_is_the_key(self):
        self.assertEqual(parse()[0], REAL["lxmf"])
        self.assertEqual(parse(name="renamed", id=1)[0], REAL["lxmf"])

    def test_zero_or_missing_lxmf_falls_back_to_id(self):
        self.assertEqual(parse(lxmf="0" * 32)[0], f"id:{REAL['id']}")
        self.assertEqual(parse(lxmf="")[0], f"id:{REAL['id']}")
        # two unconfigured nodes must not collapse onto one marker
        self.assertNotEqual(parse(lxmf="0" * 32, id=1)[0], parse(lxmf="0" * 32, id=2)[0])

    def test_no_id_falls_back_to_name(self):
        d = dict(REAL, lxmf="")
        del d["id"]
        key, _ = H.parse_telemetry(json.dumps(d).encode(), NOW)
        self.assertEqual(key, "name:Catz Node (Halow)")


if __name__ == "__main__":
    unittest.main()
