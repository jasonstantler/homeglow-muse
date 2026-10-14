#!/usr/bin/env bash
# Installs the HomeGlow wall bridge next to the Muse gadget service.
#
#   sudo bash install-bridge.sh              install or update
#   sudo bash install-bridge.sh --show-key   print the address and wall key again
#   sudo bash install-bridge.sh --new-key    make a new wall key (the card needs the new one)
#   sudo bash install-bridge.sh --uninstall  go back to the plain Muse gadget service
#
# Needs Meta's Muse gadget installed first (it lives in /opt/musegadget).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=/opt/homeglow-muse
CONF_DIR=/etc/homeglow-muse
CONF="$CONF_DIR/config.json"
DROPIN_DIR=/etc/systemd/system/musegadget.service.d
DROPIN="$DROPIN_DIR/homeglow-muse.conf"
GADGET_PY=/opt/musegadget/venv/bin/python
PORT=8791

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf 'Stopped: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run this with sudo: sudo bash install-bridge.sh"

address() {
    local ip
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' | head -n1)"
    [ -n "$ip" ] || ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    local port
    port="$("$GADGET_PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port", 8791))' "$CONF")"
    printf 'http://%s:%s' "${ip:-THIS-VM-ADDRESS}" "$port"
}

show_key() {
    [ -f "$CONF" ] || die "the bridge isn't installed yet"
    local key
    key="$("$GADGET_PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["key"])' "$CONF")"
    echo
    say "Put these in the Muse card's Plugin Options in HomeGlow:"
    echo "  Bridge address:  $(address)"
    echo "  Wall key:        $key"
    echo
}

new_config() {
    install -d -m 0700 "$CONF_DIR"
    "$GADGET_PY" - "$CONF" "$PORT" "${1:-}" <<'PY'
import json, os, secrets, sys, uuid
path, port, keep_chat = sys.argv[1], int(sys.argv[2]), sys.argv[3]
old = {}
if os.path.exists(path):
    with open(path) as f:
        old = json.load(f)
cfg = {
    "port": old.get("port", port),
    "key": secrets.token_urlsafe(24),
    "chat_id": old.get("chat_id") or str(uuid.uuid4()),
    "allow_commands": old.get("allow_commands", False),
}
fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(path + ".tmp", path)
PY
}

case "${1:-}" in
    --show-key) show_key; exit 0 ;;
    --new-key)
        new_config
        systemctl restart musegadget.service
        say "Made a new wall key. Update the Muse card in HomeGlow:"
        show_key; exit 0 ;;
    --uninstall)
        rm -f "$DROPIN"
        systemctl daemon-reload
        systemctl restart musegadget.service || true
        say "Removed the bridge. The Muse gadget runs on its own again."
        echo "(Your settings are still in $CONF; delete that folder to forget them.)"
        exit 0 ;;
    "") ;;
    *) die "unknown option $1" ;;
esac

[ -x "$GADGET_PY" ] || die "install Meta's Muse gadget first (step 3 of the guide); /opt/musegadget is missing"
[ -f "$HERE/bridge.py" ] || die "bridge.py must sit next to this script"
"$GADGET_PY" -c 'import musegadget.muse_turn, musegadget.service' 2>/dev/null \
    || die "this version of the Muse gadget doesn't have what the bridge needs; update it and try again"

say "Installing the bridge into $PREFIX"
install -d -m 0755 "$PREFIX"
install -m 0644 "$HERE/bridge.py" "$PREFIX/bridge.py"
"$GADGET_PY" -m py_compile "$PREFIX/bridge.py"

if [ ! -f "$CONF" ]; then
    say "Making a wall key"
    new_config
fi

say "Starting the gadget with the bridge"
install -d -m 0755 "$DROPIN_DIR"
cat > "$DROPIN" <<EOF
# Added by the HomeGlow wall bridge. Remove with: sudo bash install-bridge.sh --uninstall
[Service]
ExecStart=
ExecStart=$GADGET_PY $PREFIX/bridge.py
EOF
systemctl daemon-reload
systemctl restart musegadget.service

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    port="$("$GADGET_PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port", 8791))' "$CONF")"
    say "Opening port $port for your home network in the firewall"
    ufw allow from 192.168.0.0/16 to any port "$port" proto tcp >/dev/null
fi

sleep 3
if systemctl is-active --quiet musegadget.service; then
    say "The bridge is running."
else
    echo "The service didn't stay up. See why with: sudo journalctl -u musegadget -n 50"
fi
show_key
