"""
NOEMA interface profiles: named sets of Reticulum interfaces that are switched on together.

A profile lists the interfaces that must be enabled; applying it enables those and disables every
other interface (for example "Home": radio + internet hubs, "Hike": radio only). Only the `enabled`
line of the affected [[interface]] sections of the Reticulum config is changed, everything else in the
file stays as it is, and a backup copy is written first. The stack reads the config at start, so the
dashboard has to be restarted afterwards.

Routes (registered with register_routes):
    GET  /api/profiles                 profiles, interfaces (name, type, enabled), active profile
    POST /api/profiles/save            {"name", "members": [interface names]}   create or change a profile
    POST /api/profiles/delete          {"name"}
    POST /api/profiles/preview         {"name"}   what applying would change (nothing is written)
    POST /api/profiles/apply           {"name", "confirm": true}   write the config (backup first)
"""

import json
import os
import re
import shutil
import threading
import time

HEADER = re.compile(r"^(\s*)\[\[(.+?)\]\]\s*$")
ANY_HEADER = re.compile(r"^\s*\[")
KEY = re.compile(r"^(\s*)(enabled|interface_enabled)(\s*=\s*)([^#\n]*?)(\s*(#.*)?)$", re.I)
TYPE = re.compile(r"^\s*type\s*=\s*([^#\n]+?)\s*(#.*)?$", re.I)
TRUE = {"yes", "true", "1", "on"}


def parse(text):
    """[{name, type, enabled, header, end, key}] for every [[interface]] section; line numbers are indices."""
    lines = text.split("\n")
    out, cur = [], None
    for n, line in enumerate(lines):
        h = HEADER.match(line)
        if h or ANY_HEADER.match(line):
            if cur:
                cur["end"] = n
                out.append(cur)
                cur = None
            if h:
                cur = {"name": h.group(2).strip(), "type": "", "enabled": True, "header": n, "indent": h.group(1), "end": len(lines), "key": None}
            continue
        if cur and not line.lstrip().startswith("#"):
            k = KEY.match(line)
            if k and cur["key"] is None:
                cur["key"] = n
                cur["enabled"] = k.group(4).strip().lower() in TRUE
            t = TYPE.match(line)
            if t and not cur["type"]:
                cur["type"] = t.group(1).strip()
    if cur:
        out.append(cur)
    return out


def apply_enabled(text, wanted):
    """Return (new_text, changes) with the `enabled` state of the named interfaces set as wanted ({name: bool})."""
    lines = text.split("\n")
    changes, inserts = [], []
    for s in parse(text):
        if s["name"] not in wanted or wanted[s["name"]] == s["enabled"]:
            continue
        value = "yes" if wanted[s["name"]] else "no"
        if s["key"] is not None:
            k = KEY.match(lines[s["key"]])
            lines[s["key"]] = k.group(1) + k.group(2) + k.group(3) + value + k.group(5)
        else:
            inserts.append((s["header"] + 1, s["indent"] + "  enabled = " + value))
        changes.append({"name": s["name"], "from": s["enabled"], "to": wanted[s["name"]]})
    for pos, line in sorted(inserts, reverse=True):       # later positions first, so earlier indices stay valid
        lines.insert(pos, line)
    return "\n".join(lines), changes


class Profiles:
    def __init__(self, home, base_dir):
        self.cfg = os.path.join(home, ".reticulum", "config")
        self.store = os.path.join(base_dir, "profiles.json")
        self.lock = threading.Lock()

    def _read_cfg(self):
        with open(self.cfg) as f:
            return f.read()

    def interfaces(self):
        return [{"name": s["name"], "type": s["type"], "enabled": s["enabled"]} for s in parse(self._read_cfg())]

    def _load(self):
        try:
            with open(self.store) as f:
                d = json.load(f)
            return d.get("profiles") if isinstance(d.get("profiles"), dict) else None
        except Exception:
            return None

    def _save(self, profiles):
        os.makedirs(os.path.dirname(self.store), exist_ok=True)
        tmp = self.store + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"profiles": profiles}, f, ensure_ascii=False)
        os.replace(tmp, self.store)

    def listing(self):
        ifs = self.interfaces()
        with self.lock:
            profiles = self._load()
            if profiles is None:        # first use: two example profiles made from the current state
                on = [i["name"] for i in ifs if i["enabled"]]
                profiles = {"Дом": on, "Поход": [i["name"] for i in ifs if i["enabled"] and "rnode" in i["type"].lower()]}
                self._save(profiles)
        enabled = {i["name"] for i in ifs if i["enabled"]}
        active = next((n for n, m in profiles.items() if set(m) == enabled), None)
        return {"profiles": [{"name": n, "members": m} for n, m in profiles.items()], "interfaces": ifs, "active": active}

    @staticmethod
    def _name(name):
        name = (name or "").strip()
        if not name or len(name) > 40 or any(ord(c) < 32 for c in name):
            raise ValueError("bad profile name")
        return name

    def save(self, name, members):
        name = self._name(name)
        known = {i["name"] for i in self.interfaces()}
        members = [m for m in dict.fromkeys(members or []) if m in known]
        with self.lock:
            profiles = self._load() or {}
            profiles[name] = members
            self._save(profiles)
        return members

    def delete(self, name):
        with self.lock:
            profiles = self._load() or {}
            profiles.pop(name, None)
            self._save(profiles)

    def _plan(self, name):
        with self.lock:
            profiles = self._load() or {}
        if name not in profiles:
            raise ValueError("unknown profile")
        text = self._read_cfg()
        wanted = {s["name"]: s["name"] in set(profiles[name]) for s in parse(text)}
        return text, wanted

    def preview(self, name):
        text, wanted = self._plan(name)
        return apply_enabled(text, wanted)[1]

    def apply(self, name):
        text, wanted = self._plan(name)
        new, changes = apply_enabled(text, wanted)
        backup = None
        if changes:
            backup = self.cfg + ".bak-profile-" + time.strftime("%Y%m%d_%H%M%S")
            shutil.copy2(self.cfg, backup)
            tmp = self.cfg + ".tmp"
            with open(tmp, "w") as f:
                f.write(new)
            os.replace(tmp, self.cfg)
        return changes, backup


def register_routes(app, home, base_dir, prefix="/api/profiles"):
    from flask import jsonify, request
    pr = Profiles(home, base_dir)

    def body():
        return request.get_json(silent=True) or {}

    def guard(fn):
        try:
            return fn()
        except (ValueError, OSError) as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    @app.route(prefix)
    def profiles_list():
        return guard(lambda: jsonify(pr.listing()))

    @app.route(prefix + "/save", methods=["POST"])
    def profiles_save():
        return guard(lambda: jsonify({"ok": True, "members": pr.save(body().get("name"), body().get("members"))}))

    @app.route(prefix + "/delete", methods=["POST"])
    def profiles_delete():
        return guard(lambda: (pr.delete(body().get("name")), jsonify({"ok": True}))[1])

    @app.route(prefix + "/preview", methods=["POST"])
    def profiles_preview():
        return guard(lambda: jsonify({"ok": True, "changes": pr.preview(body().get("name"))}))

    @app.route(prefix + "/apply", methods=["POST"])
    def profiles_apply():
        def run():
            if body().get("confirm") is not True:
                raise ValueError("confirmation required")
            changes, backup = pr.apply(body().get("name"))
            return jsonify({"ok": True, "changes": changes, "backup": backup})
        return guard(run)

    return pr
