#!/usr/bin/env bash
# Move this machine's Muse pairing to Muse Link, the Home Assistant add-on.
#
# One pairing can only run in one place, so this turns off Muse on this
# machine (the musegadget service, and the wall bridge if it's installed),
# then prints a code to paste into the Muse Link page in Home Assistant.
#
#   curl -fsSL https://raw.githubusercontent.com/jasonstantler/homeglow-muse/main/bridge/move-pairing.sh | sudo bash
#
# To move it back later, stop Muse Link in Home Assistant first, then run
#   sudo systemctl enable --now musegadget

set -euo pipefail

STATE_DIR="${MUSEGADGET_STATE_DIR:-/var/lib/musegadget}"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo." >&2
    exit 1
fi
if [ ! -s "$STATE_DIR/pairing.json" ] || [ ! -s "$STATE_DIR/identity.json" ]; then
    echo "This machine isn't paired with Muse: there's no pairing in $STATE_DIR." >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "This needs python3, which the Muse gadget installer adds. Is Muse installed here?" >&2
    exit 1
fi

# Stop it first, so it can't renew its sign-in after the code is made.
if command -v systemctl >/dev/null 2>&1 && systemctl cat musegadget.service >/dev/null 2>&1; then
    systemctl disable --now musegadget.service >/dev/null 2>&1 || true
    echo "Turned off Muse on this machine (the musegadget service)."
fi

python3 - "$STATE_DIR" <<'PY'
import base64
import json
import pathlib
import sys
import textwrap

state = pathlib.Path(sys.argv[1])
identity = json.loads((state / "identity.json").read_text(encoding="utf-8"))
pairing = json.loads((state / "pairing.json").read_text(encoding="utf-8"))
if not isinstance(identity, dict) or not identity.get("mac"):
    sys.exit("The saved identity here is damaged, so there's nothing to move.")
if not isinstance(pairing, dict) or not pairing.get("refresh_token"):
    sys.exit("The saved pairing here is damaged, so there's nothing to move.")

bundle = {"v": 1, "identity": {"mac": identity["mac"]}, "pairing": pairing}
code = base64.b64encode(json.dumps(bundle, separators=(",", ":")).encode()).decode()
name = "MuseGadget" + identity["mac"].replace(":", "")[-6:].upper()

print()
print("In Home Assistant, open Muse Link, choose 'Move a pairing here from another")
print("machine', and paste everything from BEGIN to END:")
print()
print("-----BEGIN MUSE PAIRING-----")
print("\n".join(textwrap.wrap(code, 64)))
print("-----END MUSE PAIRING-----")
print()
print("Muse Link will then be " + name + ", the same device as in your Muse app.")
print("This code works like a password for your Muse: don't share it or post it.")
print("When you're done, run 'clear' to take it off the screen.")
PY
