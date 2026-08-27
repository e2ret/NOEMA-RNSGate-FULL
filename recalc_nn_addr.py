#!/usr/bin/env python3
import os, sys, time
HOME = os.path.expanduser("~")
id_file = f"{HOME}/.nomadnetwork/storage/identity"
for _ in range(30):
    if os.path.exists(id_file):
        break
    time.sleep(2)
try:
    import RNS
    RNS.Reticulum(configdir=f"{HOME}/.reticulum", loglevel=RNS.LOG_CRITICAL)
    id = RNS.Identity.from_file(id_file)
    dest = RNS.Destination(id, RNS.Destination.IN, RNS.Destination.SINGLE, "nomadnetwork", "node")
    addr = RNS.hexrep(dest.hash, delimit=False)
    open(f"{HOME}/.nomadnetwork/node_address","w").write(addr)
    print(f"OK: {addr}")
except Exception as e:
    print(f"ERROR: {e}")
