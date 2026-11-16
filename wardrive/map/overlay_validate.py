"""
overlay_validate.py - parse and vet signal-overlay exports uploaded to the public map.

An export is an unsigned, user-editable file, so nothing here can prove a measurement
is real. What it does is reject everything that cannot be real (impossible values,
teleporting tracks, timestamps from the future) and everything that is not the app's
own output (inconsistent replied/rssi, malformed ids), so that faking a believable
batch takes deliberate effort per row. Who sent a batch, how much they may send, and
undoing it afterwards are overlay_store.py's job.

The server parses the raw upload itself; rows normalised by a client are never trusted.
Stdlib only: the map host's python carries no extra packages.
"""

import csv
import io
import json
import math
import re
import calendar
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass

DAY_MS = 86400 * 1000

# Firmware/app protocol bounds (RNode_Firmware/Documentation/WARDRIVE_PROTOCOL.md):
# ul_rssi is an i16 dBm, ul_snr an i8 in quarter-dB units.
RSSI_MIN, RSSI_MAX = -140.0, 0.0
SNR_MIN, SNR_MAX = -32.0, 31.75
SEQ_MAX = 0xFFFF

_HEX8 = re.compile(r"^[0-9a-f]{8}$")
_ISO = re.compile(
    r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:?\d\d)?$")
_GPX_SESSION = re.compile(r"session ([0-9a-fA-F]{8})")
_BAD = object()          # a value that is present but unusable (absent is None)


@dataclass
class Limits:
    max_rows: int = 20000              # raw rows in one upload
    max_age_days: int = 366            # older samples are not current coverage
    max_span_days: int = 60            # first to last sample in one upload
    future_slack_ms: int = 5 * 60 * 1000
    max_accuracy_m: float = 50.0       # worse GPS fixes smear the heatmap
    max_speed_ms: float = 80.0         # ~290 km/h; faster is a jump, not a drive
    still_radius_m: float = 30.0       # two fixes at the same instant may differ this much
    max_reject_frac: float = 0.2       # more invalid rows than this sinks the whole upload
    min_ts_ms: int = 1640995200000     # 2022-01-01; the app did not exist before this
    synthetic_min_rows: int = 50       # constant rssi AND snr over this many replies


class ParseError(ValueError):
    """The upload is not a readable export. args[0] is a short machine-readable reason."""


@dataclass
class BatchResult:
    rows: list
    rejected: Counter
    duplicates: int
    raw_count: int
    batch_error: str = None


# ---- parsing -------------------------------------------------------------------

def parse_export(name, data, lim=None):
    """Raw upload bytes -> list of raw record dicts, in the browser parser's shape."""
    lim = lim or Limits()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ParseError("not_utf8")
    body = text.lstrip()
    ext = (name or "").lower().rsplit(".", 1)[-1]
    if ext == "csv" or body.startswith("time_") or ",lat," in body:
        return _parse_csv(body, lim)
    if ext == "gpx" or body.startswith("<"):
        return _parse_gpx(body, lim)
    return _parse_geojson(body, lim)


def _row_cap(n, lim):
    if n > lim.max_rows:
        raise ParseError("too_many_rows")


def _parse_geojson(text, lim=None):
    lim = lim or Limits()
    try:
        fc = json.loads(text)
    except (ValueError, RecursionError):
        raise ParseError("bad_json")
    if not isinstance(fc, dict):
        raise ParseError("bad_geojson")
    feats = fc.get("features") if fc.get("type") == "FeatureCollection" else [fc]
    if not isinstance(feats, list):
        raise ParseError("bad_geojson")
    _row_cap(len(feats), lim)
    out = []
    for ft in feats:
        geom = ft.get("geometry") if isinstance(ft, dict) else None
        coords = geom.get("coordinates") if isinstance(geom, dict) and geom.get("type") == "Point" else None
        if isinstance(coords, list) and len(coords) >= 2:
            props = ft.get("properties")
            out.append({**(props if isinstance(props, dict) else {}),
                        "lon": coords[0], "lat": coords[1]})
        else:
            out.append({})
    return out


def _parse_csv(text, lim=None):
    lim = lim or Limits()
    try:
        recs = [r for r in csv.reader(io.StringIO(text, newline="")) if r]
    except csv.Error:
        raise ParseError("bad_csv")
    if not recs:
        return []
    head = [h.strip() for h in recs[0]]
    _row_cap(len(recs) - 1, lim)
    return [{h: (r[i] if i < len(r) else None) for i, h in enumerate(head)} for r in recs[1:]]


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _parse_gpx(text, lim=None):
    lim = lim or Limits()
    # A DTD is the only way to get entity expansion out of the stdlib parser, and an
    # export never has one.
    if re.search(r"<!(DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise ParseError("xml_dtd_forbidden")
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        raise ParseError("bad_xml")
    out = []
    for trk in root.iter():
        if _local(trk.tag) != "trk":
            continue
        session = None
        for child in trk:
            if _local(child.tag) == "name":
                m = _GPX_SESSION.search(child.text or "")
                session = m.group(1) if m else None
        for pt in trk.iter():
            if _local(pt.tag) != "trkpt":
                continue
            _row_cap(len(out) + 1, lim)
            rec = {"lat": pt.get("lat"), "lon": pt.get("lon"), "session": session}
            for el in pt.iter():
                key = _local(el.tag)
                if key == "time":
                    rec["time"] = el.text
                elif key == "responder":
                    rec["responder_id"] = el.text
                elif key in ("replied", "seq", "accuracy_m", "ul_rssi_dbm", "ul_snr_db",
                             "dl_rssi_dbm", "dl_snr_db"):
                    rec[key] = el.text
            out.append(rec)
    return out


# ---- per-row -------------------------------------------------------------------

def _num(v):
    """None when absent, _BAD when present but not a finite number, else a float."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return _BAD
    if isinstance(v, (int, float)):
        try:
            x = float(v)
        except OverflowError:       # a JSON integer with thousands of digits (python < 3.11 parses those)
            return _BAD
    elif isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            x = float(s)
        except ValueError:
            return _BAD
    else:
        return _BAD
    return x if math.isfinite(x) else _BAD


def _iso_ms(s):
    """ISO-8601 -> epoch ms, or None. Hand-rolled so it does not depend on the python version."""
    m = _ISO.match((s or "").strip()) if isinstance(s, str) else None
    if not m:
        return None
    y, mo, d, h, mi, sec, frac, tz = m.groups()
    try:
        t = calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), int(sec)))
    except (ValueError, OverflowError):
        return None
    if int(mo) > 12 or int(d) > 31 or int(h) > 23 or int(mi) > 59 or int(sec) > 60:
        return None
    ms = t * 1000 + (int((frac + "000")[:3]) if frac else 0)
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        ms -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60) * 1000
    return ms


def normalise_row(f, now_ms, lim):
    """One raw record -> (row dict, None) or (None, reason)."""
    lat, lon = _num(f.get("lat")), _num(f.get("lon"))
    if lat is None or lon is None:
        return None, "missing_position"
    if lat is _BAD or lon is _BAD or abs(lat) > 90 or abs(lon) > 180:
        return None, "bad_position"
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:        # a GPS with no fix reports 0,0
        return None, "null_island"

    ms = _num(f.get("time_ms"))
    stamp = _iso_ms(f.get("time") or f.get("time_utc"))
    if ms is _BAD:
        return None, "bad_time"
    if ms is None:
        if stamp is None:
            return None, "missing_time"
        ms = stamp
    elif stamp is not None and abs(stamp - ms) > 2000:
        return None, "time_mismatch"               # the two spellings must agree
    ms = int(ms)
    if ms > now_ms + lim.future_slack_ms:
        return None, "time_future"
    if ms < max(lim.min_ts_ms, now_ms - lim.max_age_days * DAY_MS):
        return None, "time_old"

    def signal(*names, lo, hi):
        for n in names:
            if f.get(n) is not None and f.get(n) != "":
                x = _num(f.get(n))
                if x is None:
                    return None
                return x if x is not _BAD and lo <= x <= hi else _BAD
        return None
    rssi = signal("rssi", "ul_rssi_dbm", lo=RSSI_MIN, hi=RSSI_MAX)
    snr = signal("snr", "ul_snr_db", lo=SNR_MIN, hi=SNR_MAX)
    dl_rssi = signal("dl_rssi", "dl_rssi_dbm", lo=RSSI_MIN, hi=RSSI_MAX)
    dl_snr = signal("dl_snr", "dl_snr_db", lo=SNR_MIN, hi=SNR_MAX)
    if _BAD in (rssi, snr, dl_rssi, dl_snr):
        return None, "bad_signal"

    who = f.get("responder_id")
    who = str(who).strip().lower() if who is not None else ""
    if who and not _HEX8.match(who):
        return None, "bad_responder"

    flag = f.get("replied")
    flag = str(flag).strip().lower() if flag is not None else ""
    if flag == "true":
        replied = True
    elif flag == "false":
        replied = False
    elif who:
        replied = True
    else:
        return None, "no_signal_data"              # a bare track point is not a measurement
    if replied and (not who or rssi is None):
        return None, "inconsistent"
    if not replied and (who or rssi is not None or snr is not None
                        or dl_rssi is not None or dl_snr is not None):
        return None, "inconsistent"

    acc = _num(f.get("accuracy_m"))
    if acc is _BAD or (acc is not None and not 0 <= acc <= lim.max_accuracy_m):
        return None, "poor_accuracy"

    session = f.get("session")
    session = str(session).strip().lower() if session is not None else ""
    if session and not _HEX8.match(session):
        return None, "bad_session"
    seq = _num(f.get("seq"))
    if seq is _BAD or (seq is not None and (seq != int(seq) or not 0 <= seq <= SEQ_MAX)):
        return None, "bad_seq"
    seq = int(seq) if seq is not None else None

    lat_e6, lon_e6 = round(lat * 1e6), round(lon * 1e6)
    key = (f"{session}:{seq}:{who}" if session and seq is not None
           else f"t:{ms}:{lat_e6}:{lon_e6}")
    return {"ts_ms": ms, "lat_e6": lat_e6, "lon_e6": lon_e6, "replied": replied,
            "responder": who, "rssi": rssi, "snr": snr, "dl_rssi": dl_rssi, "dl_snr": dl_snr,
            "accuracy_m": acc, "session": session, "dedup_key": key}, None


def _dist_m(a, b):
    mean = math.radians((a["lat_e6"] + b["lat_e6"]) / 2e6)
    x = math.radians((b["lon_e6"] - a["lon_e6"]) / 1e6) * math.cos(mean)
    y = math.radians((b["lat_e6"] - a["lat_e6"]) / 1e6)
    return 6371000.0 * math.hypot(x, y)


# ---- whole upload --------------------------------------------------------------

def validate_batch(records, now_ms, lim=None):
    """Raw records -> BatchResult. Rows that pass are in .rows, oldest first."""
    lim = lim or Limits()
    rejected = Counter()
    dup = 0
    seen = set()
    good = []
    for rec in records:
        row, why = normalise_row(rec if isinstance(rec, dict) else {}, now_ms, lim)
        if row is None:
            rejected[why] += 1
        elif row["dedup_key"] in seen:
            dup += 1                               # overlapping exports are normal, not abuse
        else:
            seen.add(row["dedup_key"])
            good.append(row)

    # A track cannot jump. Compared against the last row kept, so one bad fix costs one row.
    kept = []
    last = {}
    for row in sorted(good, key=lambda r: r["ts_ms"]):
        prev = last.get(row["session"])
        if prev is not None:
            dt = (row["ts_ms"] - prev["ts_ms"]) / 1000.0
            d = _dist_m(prev, row)
            if (dt <= 0 and d > lim.still_radius_m) or (dt > 0 and d / dt > lim.max_speed_ms):
                rejected["speed"] += 1
                continue
        last[row["session"]] = row
        kept.append(row)

    result = BatchResult(kept, rejected, dup, len(records))
    considered = len(records) - dup
    failed = sum(rejected.values())
    if not kept:
        result.batch_error = "no_valid_rows"
    elif failed > lim.max_reject_frac * considered:
        result.batch_error = "too_many_invalid"
    elif kept[-1]["ts_ms"] - kept[0]["ts_ms"] > lim.max_span_days * DAY_MS:
        result.batch_error = "span_too_long"
    else:
        replies = [r for r in kept if r["replied"]]
        if (len(replies) >= lim.synthetic_min_rows
                and len({r["rssi"] for r in replies}) == 1
                and len({r["snr"] for r in replies}) == 1):
            result.batch_error = "synthetic"
    return result
