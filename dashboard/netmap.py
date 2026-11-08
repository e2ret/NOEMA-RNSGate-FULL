"""
NOEMA network map backend: where do this gateway's Reticulum interfaces connect to?

Reads the interface list from ~/.reticulum/config (TCP / Backbone client interfaces
with a target_host), resolves each host to an IP and looks the IP up in an offline
GeoIP database (DB-IP "City Lite", downloaded on request from the dashboard).
No IP address is ever sent to a third-party lookup service.

Routes (registered with register_routes):
    GET  /api/netmap/data          interfaces + their location, origin, db status
    GET  /api/netmap/settings      {"origin": {"lat", "lon", "label"} | null}
    POST /api/netmap/settings      set the gateway's own position
    GET  /api/netmap/db            database status / download progress
    POST /api/netmap/db/download   start the (user-initiated) database download
"""

import bisect
import ipaddress
import json
import mmap
import os
import re
import shutil
import socket
import struct
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

try:
    import maxminddb
except Exception as _e:  # not installed -> map still lists links, just without positions
    maxminddb = None
    DEPS_ERROR = str(_e)
else:
    DEPS_ERROR = None

DB_NAME = "dbip-city-lite.mmdb"
NS_NAME = "ipcity.bin"          # compact format shipped with Noema Snitch (also DB-IP City Lite)
NS_MAGIC = b"NSGEO1\0\0"
DB_URL = "https://download.db-ip.com/free/dbip-city-lite-{ym}.mmdb.gz"
DB_MIN_FREE = 400 * 1024 * 1024
DL_ATTEMPTS = 4
LINK_TYPES = {"TCPClientInterface", "BackboneClientInterface", "BackboneInterface"}
RESOLVE_TTL = 600
_TRUE = {"yes", "true", "1", "on"}


# ---------------------------------------------------------------------------
# Reticulum config
# ---------------------------------------------------------------------------

def parse_interfaces(text):
    """[[Name]] sections of a Reticulum config -> [{"name", "params"}]."""
    out = []
    for m in re.finditer(r"^\s*\[\[([^\]]+)\]\](.*?)(?=^\s*\[\[|\Z)", text, re.MULTILINE | re.DOTALL):
        params = {}
        for line in m.group(2).splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                params[k.strip()] = v.split("#", 1)[0].strip()
        out.append({"name": m.group(1).strip(), "params": params})
    return out


def link_interfaces(text):
    """Interfaces that connect *out* to a remote host (have a target_host)."""
    links = []
    for itf in parse_interfaces(text):
        p = itf["params"]
        if p.get("type") in LINK_TYPES and p.get("target_host"):
            try:
                port = int(p.get("target_port", "0"))
            except ValueError:
                port = 0
            links.append({
                "name": itf["name"], "type": p["type"], "host": p["target_host"], "port": port,
                "enabled": p.get("enabled", p.get("interface_enabled", "yes")).lower() in _TRUE,
            })
    return links


# ---------------------------------------------------------------------------
# Geolocation
# ---------------------------------------------------------------------------

def is_public(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified)


class NsGeo:
    """Reader for ipcity.bin (the compact DB-IP City Lite format of Noema Snitch).

    "NSGEO1\\0\\0"; u32 city count; per city: f32 lat, f32 lon, 2-byte country, u16 name
    length, UTF-8 name; u32 v4 count, u32 starts[], u32 city[]; u32 v6 count, u64 starts
    (upper 64 bits)[], u32 city[]. City 0xFFFFFFFF = unknown range. Little-endian.
    The big tables are memory-mapped, so they cost almost no RAM.
    """

    UNKNOWN = 0xFFFFFFFF

    def __init__(self, path):
        if sys.byteorder != "little":
            raise ValueError("ipcity.bin needs a little-endian machine")
        self._fh = open(path, "rb")
        try:
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
            mm = self._mm
            if mm[:8] != NS_MAGIC:
                raise ValueError("not an ipcity.bin file")
            (n,) = struct.unpack_from("<I", mm, 8)
            off = 12
            self.cities = []
            for _ in range(n):
                lat, lon = struct.unpack_from("<ff", mm, off)
                cc = bytes(mm[off + 8:off + 10]).decode("ascii", "replace")
                (ln,) = struct.unpack_from("<H", mm, off + 10)
                name = bytes(mm[off + 12:off + 12 + ln]).decode("utf-8", "replace")
                self.cities.append((lat, lon, cc, name))
                off += 12 + ln
            (c4,) = struct.unpack_from("<I", mm, off); off += 4
            view = memoryview(mm)
            self._v4s = view[off:off + 4 * c4].cast("I"); off += 4 * c4
            self._v4c = view[off:off + 4 * c4].cast("I"); off += 4 * c4
            (c6,) = struct.unpack_from("<I", mm, off); off += 4
            self._v6s = view[off:off + 8 * c6].cast("Q"); off += 8 * c6
            self._v6c = view[off:off + 4 * c6].cast("I"); off += 4 * c6
            if off != len(mm) or len(self._v4c) != c4 or len(self._v6c) != c6:
                raise ValueError("ipcity.bin is truncated or damaged")
        except Exception as e:
            self.close()
            if isinstance(e, ValueError):
                raise
            raise ValueError("ipcity.bin is truncated or damaged") from e

    def close(self):
        for name in ("_v4s", "_v4c", "_v6s", "_v6c"):
            v = getattr(self, name, None)
            if v is not None:
                v.release()
                setattr(self, name, None)
        mm = getattr(self, "_mm", None)
        if mm is not None:
            try:
                mm.close()
            except Exception:
                pass
        self._fh.close()

    def get(self, ip):
        a = ipaddress.ip_address(ip)
        if a.version == 4:
            starts, owner, key = self._v4s, self._v4c, int(a)
        else:
            starts, owner, key = self._v6s, self._v6c, int(a) >> 64
        i = bisect.bisect_right(starts, key) - 1
        if i < 0:
            return None
        c = owner[i]
        if c == self.UNKNOWN or c >= len(self.cities):
            return None
        lat, lon, cc, name = self.cities[c]
        return {"lat": float(lat), "lon": float(lon), "city": name, "country": cc, "cc": cc}


class Geo:
    def __init__(self, db_path, ns_path=None):
        self.db_path = db_path                                   # MaxMind .mmdb
        self.ns_path = ns_path or os.path.join(os.path.dirname(db_path), NS_NAME)
        self._reader = None
        self._ns = None
        self._lock = threading.Lock()

    def present(self):
        return os.path.isfile(self.ns_path) or os.path.isfile(self.db_path)

    def format(self):
        if os.path.isfile(self.ns_path):
            return "ipcity"
        return "mmdb" if os.path.isfile(self.db_path) else None

    def close(self):
        with self._lock:
            for obj in (self._reader, self._ns):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass
            self._reader = self._ns = None

    def lookup(self, ip):
        """-> (geo | None, reason | None)"""
        if not is_public(ip):
            return None, "private"
        fmt = self.format()
        if fmt is None:
            return None, "no_db"
        with self._lock:
            try:
                if fmt == "ipcity":
                    if self._ns is None:
                        self._ns = NsGeo(self.ns_path)
                    geo = self._ns.get(ip)
                    return (geo, None) if geo else (None, "not_found")
                if maxminddb is None:
                    return None, "no_lib"
                if self._reader is None:
                    self._reader = maxminddb.open_database(self.db_path)
                rec = self._reader.get(ip)
            except Exception:
                return None, "db_error"
        loc = (rec or {}).get("location") or {}
        if loc.get("latitude") is None or loc.get("longitude") is None:
            return None, "not_found"
        names = lambda d: ((d or {}).get("names") or {}).get("en", "")
        return {
            "lat": float(loc["latitude"]), "lon": float(loc["longitude"]),
            "city": names((rec or {}).get("city")),
            "country": names((rec or {}).get("country")),
            "cc": ((rec or {}).get("country") or {}).get("iso_code", ""),
        }, None


class Resolver:
    def __init__(self):
        self._cache = {}
        self._pool = ThreadPoolExecutor(max_workers=4)

    def resolve(self, host, timeout=3.0):
        try:
            ipaddress.ip_address(host)
            return host
        except ValueError:
            pass
        hit = self._cache.get(host)
        if hit and time.time() - hit[1] < RESOLVE_TTL:
            return hit[0]
        fut = self._pool.submit(socket.getaddrinfo, host, None, 0, socket.SOCK_STREAM)
        try:
            infos = fut.result(timeout=timeout)
            ips = [i[4][0] for i in infos]
            ip = next((x for x in ips if ":" not in x), ips[0])  # prefer IPv4 (better GeoIP coverage)
        except Exception:
            ip = None
        self._cache[host] = (ip, time.time())
        return ip


# ---------------------------------------------------------------------------
# Database download (always started by the user from the UI)
# ---------------------------------------------------------------------------

class DbDownloader:
    def __init__(self, geo_dir, geo):
        self.geo_dir, self.geo = geo_dir, geo
        self.state = {"state": "idle", "done": 0, "total": 0, "message": ""}
        self._lock = threading.Lock()

    def snapshot(self):
        return dict(self.state)

    def start(self):
        with self._lock:
            if self.state["state"] == "running":
                return False
            self.state = {"state": "running", "done": 0, "total": 0, "message": ""}
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _months(self):
        t = time.gmtime()
        y, m = t.tm_year, t.tm_mon
        py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
        return [f"{y:04d}-{m:02d}", f"{py:04d}-{pm:02d}"]

    def _fail(self, msg):
        self.state = {"state": "error", "done": self.state.get("done", 0), "total": self.state.get("total", 0), "message": msg}

    def _run(self):
        import requests
        os.makedirs(self.geo_dir, exist_ok=True)
        if shutil.disk_usage(self.geo_dir).free < DB_MIN_FREE:
            return self._fail("not enough free disk space (need about 400 MB)")
        tmp = os.path.join(self.geo_dir, ".download.tmp")
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
        url = None
        got = 0            # compressed bytes received so far (the resume offset)
        last_err = None
        try:
            with open(tmp, "wb") as fh:
                for attempt in range(DL_ATTEMPTS):
                    resp = None
                    try:
                        if url is None:                       # find this month's file (or last month's)
                            for ym in self._months():
                                r = requests.get(DB_URL.format(ym=ym), stream=True, timeout=(20, 180))
                                if r.status_code == 200:
                                    url, resp = DB_URL.format(ym=ym), r
                                    break
                                r.close()
                            if url is None:
                                return self._fail("database not found on the server (try again later)")
                        else:                                 # continue where the connection broke
                            resp = requests.get(url, stream=True, timeout=(20, 180), headers={"Range": f"bytes={got}-"})
                            if resp.status_code != 206:       # server cannot resume: start from scratch
                                resp.close()
                                resp = requests.get(url, stream=True, timeout=(20, 180))
                                got = 0
                                dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
                                fh.seek(0)
                                fh.truncate()
                        if not self.state["total"]:
                            self.state["total"] = int(resp.headers.get("Content-Length") or 0) + got
                        for chunk in resp.iter_content(1 << 16):
                            if not chunk:
                                continue
                            got += len(chunk)
                            self.state["done"] = got
                            fh.write(dec.decompress(chunk))
                        fh.write(dec.flush())
                        if not dec.eof:                       # server closed the stream early: resume
                            raise ConnectionError("incomplete download")
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        self.state["message"] = f"retrying ({attempt + 1}/{DL_ATTEMPTS}): {type(e).__name__}"
                        time.sleep(2)
                    finally:
                        if resp is not None:
                            resp.close()
            if last_err is not None:
                return self._fail(f"{type(last_err).__name__}: {last_err}")
            if maxminddb is not None:  # sanity check before it replaces a working database
                maxminddb.open_database(tmp).close()
            self.geo.close()
            os.replace(tmp, self.geo.db_path)
            self.state = {"state": "done", "done": got, "total": self.state["total"], "message": ""}
        except Exception as e:
            self._fail(f"{type(e).__name__}: {e}")
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Settings + Flask routes
# ---------------------------------------------------------------------------

def _clean_origin(d):
    try:
        lat, lon = float(d.get("lat")), float(d.get("lon"))
    except (TypeError, ValueError):
        raise ValueError("lat/lon must be numbers")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("lat must be -90..90 and lon -180..180")
    label = str(d.get("label") or "").strip()[:40]
    return {"lat": round(lat, 4), "lon": round(lon, 4), "label": label}


class NetMap:
    def __init__(self, base_dir, home):
        self.base_dir = base_dir
        self.cfg_path = os.path.join(home, ".reticulum", "config")
        self.settings_path = os.path.join(base_dir, "netmap.json")
        self.geo_dir = os.path.join(base_dir, "geo")
        self.geo = Geo(os.path.join(self.geo_dir, DB_NAME), os.path.join(self.geo_dir, NS_NAME))
        self.resolver = Resolver()
        self.downloader = DbDownloader(self.geo_dir, self.geo)

    def get_origin(self):
        try:
            with open(self.settings_path) as fh:
                return json.load(fh).get("origin")
        except (OSError, ValueError):
            return None

    def set_origin(self, d):
        origin = _clean_origin(d)
        os.makedirs(self.base_dir, exist_ok=True)
        tmp = self.settings_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"origin": origin}, fh)
        os.replace(tmp, self.settings_path)
        return origin

    def install_db(self, stream, max_bytes=600 * 1024 * 1024):
        """Install a database the user picked in the browser (plain .mmdb or .mmdb.gz)."""
        os.makedirs(self.geo_dir, exist_ok=True)
        if shutil.disk_usage(self.geo_dir).free < DB_MIN_FREE:
            raise ValueError("not enough free disk space (need about 400 MB)")
        tmp = os.path.join(self.geo_dir, ".upload.tmp")
        try:
            first = stream.read(2)
            dec = zlib.decompressobj(16 + zlib.MAX_WBITS) if first == b"\x1f\x8b" else None
            total = len(first)
            with open(tmp, "wb") as fh:
                chunk = first
                while chunk:
                    fh.write(dec.decompress(chunk) if dec else chunk)
                    chunk = stream.read(1 << 16)
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("file is too large")
                if dec:
                    fh.write(dec.flush())
                    if not dec.eof:
                        raise ValueError("the file is incomplete (the download was interrupted), download it again")
            with open(tmp, "rb") as fh:
                head = fh.read(8)
            if head == NS_MAGIC:                                  # ipcity.bin from Noema Snitch
                try:
                    NsGeo(tmp).close()
                except Exception as e:
                    raise ValueError(f"the ipcity.bin file is damaged: {e}")
                dest = self.geo.ns_path
            else:                                                 # MaxMind format (DB-IP .mmdb)
                if maxminddb is None:
                    raise ValueError("maxminddb is not installed")
                try:
                    maxminddb.open_database(tmp).close()
                except Exception:
                    raise ValueError("this is not a valid geo database (use dbip-city-lite .mmdb or ipcity.bin)")
                dest = self.geo.db_path
            self.geo.close()
            os.replace(tmp, dest)
        except zlib.error:
            raise ValueError("the file is damaged (could not unpack it)")
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def db_status(self):
        fmt = self.geo.format()
        path = self.geo.ns_path if fmt == "ipcity" else self.geo.db_path
        st = {"present": fmt is not None, "format": fmt, "lib": fmt == "ipcity" or maxminddb is not None,
              "lib_error": DEPS_ERROR, "download": self.downloader.snapshot()}
        if fmt:
            st["size"] = os.path.getsize(path)
            st["date"] = int(os.path.getmtime(path))
        return st

    def data(self):
        try:
            with open(self.cfg_path) as fh:
                text = fh.read()
        except OSError:
            text = ""
        links = link_interfaces(text)
        for l in links:
            ip = self.resolver.resolve(l["host"])
            l["ip"] = ip
            if ip is None:
                l["geo"], l["reason"] = None, "dns"
            else:
                l["geo"], l["reason"] = self.geo.lookup(ip)
        return {"origin": self.get_origin(), "db": self.db_status(), "links": links}


def register_routes(app, base_dir, home=None, prefix="/api/netmap"):
    from flask import jsonify, request
    nm = NetMap(base_dir, home or os.path.expanduser("~"))

    @app.route(prefix + "/data")
    def netmap_data():
        return jsonify(nm.data())

    @app.route(prefix + "/settings", methods=["GET", "POST"])
    def netmap_settings():
        if request.method == "POST":
            try:
                origin = nm.set_origin(request.get_json(silent=True) or {})
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            return jsonify({"ok": True, "origin": origin})
        return jsonify({"origin": nm.get_origin()})

    @app.route(prefix + "/db")
    def netmap_db():
        return jsonify(nm.db_status())

    @app.route(prefix + "/db/download", methods=["POST"])
    def netmap_db_download():
        if not nm.downloader.start():
            return jsonify({"ok": False, "error": "download already running"}), 409
        return jsonify({"ok": True})

    @app.route(prefix + "/db/upload", methods=["POST"])
    def netmap_db_upload():
        f = request.files.get("file")
        if f is None:
            return jsonify({"ok": False, "error": "no file"}), 400
        try:
            nm.install_db(f.stream)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True})

    return nm
