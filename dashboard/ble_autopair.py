#!/usr/bin/env python3
"""
Auto-detecting BLE pairing for an RNode, invoked by the NOEMA dashboard's
/api/ble/pair endpoint.

Enables Bluetooth + pairing mode on the RNode connected at --port, figures
out which advertised "RNode *" BLE device is the one that just started
advertising (baseline scan diff), and pairs/trusts/connects to it using a
direct BlueZ D-Bus agent -- bluetoothctl as a subprocess is too slow to
relay the RNode's very short passkey window in time.

Prints exactly one JSON line to stdout and exits 0 on success, 1 on
failure:
    {"ok": true, "mac": "AA:BB:CC:DD:EE:FF", "name": "RNode 4D9A"}
    {"ok": false, "error": "..."}
"""

import argparse
import json
import re
import sys
import threading
import time

try:
    import dbus
    import dbus.exceptions
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib
except ImportError as e:
    print(json.dumps({"ok": False, "error": f"missing dependency: {e}"}))
    sys.exit(1)

try:
    import pexpect
except ImportError:
    print(json.dumps({"ok": False, "error": "missing dependency: pexpect"}))
    sys.exit(1)

AGENT_IFACE = "org.bluez.Agent1"
AGENT_PATH = "/noema/ble_autopair_agent"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
PIN_RE = re.compile(r"Bluetooth pairing PIN is:\s*(\d+)")
RNODE_NAME_RE = re.compile(r"^RNode ")


LOG_PATH = "/tmp/ble_autopair.log"


def log(msg):
    # Step trace for diagnosing hangs; never let logging break pairing.
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def emit(payload, code):
    log(f"result {payload}")
    print(json.dumps(payload))
    sys.exit(code)


class PairSession:
    def __init__(self, mainloop):
        self.mainloop = mainloop
        self.pin = None
        self.pin_ready = threading.Event()
        self.pairing_mode_ready = threading.Event()
        self.baseline_macs = set()
        self.target_mac = None
        self.target_name = None
        self.result = {"ok": False, "error": "unknown"}


class Agent(dbus.service.Object):
    def __init__(self, bus, path, session):
        super().__init__(bus, path)
        self.session = session

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        return

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        if self.session.pin_ready.wait(timeout=12):
            return self.session.pin
        raise dbus.exceptions.DBusException("org.bluez.Error.Canceled")

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        if self.session.pin_ready.wait(timeout=12):
            return dbus.UInt32(int(self.session.pin))
        raise dbus.exceptions.DBusException("org.bluez.Error.Canceled")

    @dbus.service.method(AGENT_IFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        return

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        return

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self):
        pass


def get_managed_objects(bus):
    om = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
    return om.GetManagedObjects()


def find_adapter_path(bus):
    for path, ifaces in get_managed_objects(bus).items():
        if ADAPTER_IFACE in ifaces:
            return path
    return None


def scan_rnode_devices(bus):
    """Returns {mac: name} for every currently known BLE device named 'RNode *'."""
    found = {}
    for path, ifaces in get_managed_objects(bus).items():
        dev = ifaces.get(DEVICE_IFACE)
        if not dev:
            continue
        name = str(dev.get("Name") or dev.get("Alias") or "")
        if RNODE_NAME_RE.match(name):
            found[str(dev.get("Address"))] = name
    return found


def find_device_path(bus, mac):
    target = mac.upper()
    for path, ifaces in get_managed_objects(bus).items():
        dev = ifaces.get(DEVICE_IFACE)
        if dev and str(dev.get("Address", "")).upper() == target:
            return path
    return None


def advertising_rnodes(bus):
    """RNode devices BlueZ is hearing right now (it only reports RSSI while it hears them).
    Returns {mac: (name, rssi, paired, path)}."""
    out = {}
    for path, ifaces in get_managed_objects(bus).items():
        dev = ifaces.get(DEVICE_IFACE)
        if not dev or "RSSI" not in dev:
            continue
        name = str(dev.get("Name") or dev.get("Alias") or "")
        if RNODE_NAME_RE.match(name):
            out[str(dev.get("Address"))] = (name, int(dev["RSSI"]), bool(dev.get("Paired", False)), str(path))
    return out


def pick_target(bus, adapter, session, wait=8):
    """Find the RNode that is advertising now (the one plugged in) *before* pairing mode starts.
    If BlueZ already holds a bond for it, drop that bond so a fresh pairing can succeed --
    a stale bond makes the board reject the connection and Pair() hang."""
    cands = {}
    end = time.time() + wait
    while time.time() < end:
        cands = advertising_rnodes(bus)
        if cands:
            break
        time.sleep(0.5)
    log("advertising RNodes: " + str({m: v[:3] for m, v in cands.items()}))
    if not cands:
        return
    mac = max(cands, key=lambda m: cands[m][1])
    name, _rssi, paired, path = cands[mac]
    session.target_mac, session.target_name = mac, name
    if not paired:
        return
    try:
        adapter.RemoveDevice(path)
        log(f"removed stale bond for {mac}")
    except dbus.exceptions.DBusException as e:
        log(f"RemoveDevice failed: {e}")
        return
    end = time.time() + 8
    while time.time() < end:
        if find_device_path(bus, mac):
            return
        time.sleep(0.3)
    log("device did not reappear after removal")


def run_bluetooth_on(port, timeout):
    child = pexpect.spawn(f"rnodeconf {port} --bluetooth-on", timeout=timeout, encoding="utf-8")
    try:
        child.expect(["Enabling Bluetooth", pexpect.EOF, pexpect.TIMEOUT])
        child.expect(pexpect.EOF, timeout=timeout)
    except pexpect.TIMEOUT:
        pass
    child.close(force=True)


def run_bluetooth_pair(port, timeout, session):
    child = pexpect.spawn(f"rnodeconf {port} --bluetooth-pair", timeout=timeout, encoding="utf-8")
    try:
        idx = child.expect(["Press enter to exit", pexpect.EOF, pexpect.TIMEOUT], timeout=timeout)
    except pexpect.TIMEOUT:
        session.result["error"] = "rnodeconf pairing-mode timeout"
        return
    if idx != 0:
        session.result["error"] = "rnodeconf did not enter pairing mode (device disconnected or wrong port?)"
        return

    log("rnodeconf: pairing mode on")
    session.pairing_mode_ready.set()

    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline and not session.pin_ready.is_set():
        try:
            chunk = child.read_nonblocking(size=1024, timeout=0.1)
            buf += chunk
            m = PIN_RE.search(buf)
            if m:
                session.pin = m.group(1)
                log("passkey captured from serial")
                session.pin_ready.set()
                break
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            break

    try:
        child.sendline("")
        child.expect(pexpect.EOF, timeout=5)
    except Exception:
        pass
    child.close(force=True)


def pair_success(bus, session):
    dev_path = find_device_path(bus, session.target_mac)
    dev_obj = bus.get_object("org.bluez", dev_path)
    props = dbus.Interface(dev_obj, "org.freedesktop.DBus.Properties")
    props.Set(DEVICE_IFACE, "Trusted", True)
    dev = dbus.Interface(dev_obj, DEVICE_IFACE)

    done = {"v": False}

    def finish(*_a):
        # Pairing already succeeded -- treat as success even if this immediate
        # reconnect is slow or races with BlueZ; RNS will connect on its own.
        if done["v"]:
            return False
        done["v"] = True
        session.result = {"ok": True, "mac": session.target_mac, "name": session.target_name}
        session.mainloop.quit()
        return False

    dev.Connect(reply_handler=finish, error_handler=finish, dbus_interface=DEVICE_IFACE)
    GLib.timeout_add_seconds(5, finish)


def pair_error(session, e):
    log(f"Pair error: {e}")
    session.result = {"ok": False, "error": f"pairing failed: {e}"}
    session.mainloop.quit()


def poll_and_pair(bus, session, deadline):
    if session.target_mac is None:
        current = scan_rnode_devices(bus)
        new_macs = [mac for mac in current if mac not in session.baseline_macs]
        if new_macs:
            session.target_mac = new_macs[0]
            session.target_name = current[session.target_mac]
        elif time.time() > deadline:
            if current:
                session.target_mac = next(iter(current))
                session.target_name = current[session.target_mac]
            else:
                session.result = {"ok": False, "error": "No RNode BLE advertisement seen -- is Bluetooth on the RNode enabled and is it in range?"}
                session.mainloop.quit()
                return False
        else:
            return True  # keep polling

    if session.target_mac and not getattr(session, "_logged_target", False):
        session._logged_target = True
        log(f"target {session.target_mac} {session.target_name}")
    if not session.pairing_mode_ready.wait(timeout=0.01):
        if time.time() > deadline:
            session.result = {"ok": False, "error": "RNode never confirmed pairing mode"}
            session.mainloop.quit()
            return False
        return True

    dev_obj = bus.get_object("org.bluez", find_device_path(bus, session.target_mac))
    dev = dbus.Interface(dev_obj, DEVICE_IFACE)
    props = dbus.Interface(dev_obj, "org.freedesktop.DBus.Properties")
    try:
        if props.Get(DEVICE_IFACE, "Paired"):
            # Already bonded (e.g. an earlier attempt finished after we gave up)
            pair_success(bus, session)
            return False
    except dbus.exceptions.DBusException:
        pass
    start_pair(bus, session, dev, retries=2)
    return False


def start_pair(bus, session, dev, retries):
    log(f"Pair() call, retries left {retries}")
    # A stale or aborted attempt leaves BlueZ answering "InProgress"; cancel it
    # first, and if it still complains, wait a moment and try again.
    try:
        dev.CancelPairing()
    except dbus.exceptions.DBusException:
        pass

    def on_error(e):
        if retries > 0 and "InProgress" in str(e):
            def again():
                start_pair(bus, session, dev, retries - 1)
                return False
            GLib.timeout_add(1500, again)
        else:
            pair_error(session, e)

    dev.Pair(
        reply_handler=lambda: pair_success(bus, session),
        error_handler=on_error,
        dbus_interface=DEVICE_IFACE,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    mainloop = GLib.MainLoop()
    session = PairSession(mainloop)

    log(f"start port={args.port} timeout={args.timeout}")
    Agent(bus, AGENT_PATH, session)
    agent_manager = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
    try:
        agent_manager.RegisterAgent(AGENT_PATH, "KeyboardOnly")
    except dbus.exceptions.DBusException:
        pass  # already registered from a previous run of this script
    agent_manager.RequestDefaultAgent(AGENT_PATH)

    adapter_path = find_adapter_path(bus)
    if not adapter_path:
        emit({"ok": False, "error": "No Bluetooth adapter found"}, 1)
    adapter = dbus.Interface(bus.get_object("org.bluez", adapter_path), ADAPTER_IFACE)
    adapter_props = dbus.Interface(bus.get_object("org.bluez", adapter_path), "org.freedesktop.DBus.Properties")
    adapter_props.Set(ADAPTER_IFACE, "Powered", True)

    adapter.StartDiscovery()
    time.sleep(2)  # brief baseline window so we can tell which RNode is new
    session.baseline_macs = set(scan_rnode_devices(bus).keys())
    log(f"baseline RNode devices: {sorted(session.baseline_macs)}")

    run_bluetooth_on(args.port, timeout=15)
    pick_target(bus, adapter, session)

    t_pair = threading.Thread(target=run_bluetooth_pair, args=(args.port, args.timeout, session), daemon=True)
    t_pair.start()

    deadline = time.time() + args.timeout

    def poll():
        return poll_and_pair(bus, session, deadline)

    GLib.timeout_add(300, poll)

    def watchdog():
        if mainloop.is_running():
            if not session.result.get("ok") and session.result.get("error") == "unknown":
                session.result["error"] = "overall timeout"
            mainloop.quit()
        return False

    GLib.timeout_add_seconds(args.timeout + 15, watchdog)

    try:
        mainloop.run()
    finally:
        try:
            adapter.StopDiscovery()
        except Exception:
            pass
        try:
            agent_manager.UnregisterAgent(AGENT_PATH)
        except Exception:
            pass

    emit(session.result, 0 if session.result.get("ok") else 1)


if __name__ == "__main__":
    main()
