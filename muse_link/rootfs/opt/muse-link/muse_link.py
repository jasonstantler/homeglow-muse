#!/usr/bin/env python3
"""Muse Link: a Home Assistant add-on that connects a Muse to the HomeGlow wall card.

One process runs three things:
  * Meta's Muse gadget service, which keeps this add-on connected to your Muse;
  * the wall bridge (bridge.py) on port 8791, which the HomeGlow card talks to;
  * the setup page, shown in Home Assistant's sidebar through ingress, for
    pairing with the Muse app (or moving a pairing from another machine) and
    copying the wall card's address and key.

Muse cannot run commands or change files here: the add-on answers its health
check and refuses everything else.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import secrets
import signal
import sys
import time
import urllib.request
import uuid

DATA = os.environ.get("MUSE_LINK_DATA", "/data")
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("MUSEGADGET_STATE_DIR", os.path.join(DATA, "musegadget"))
os.environ.setdefault("MUSEGADGET_SOCKET", "/run/musegadget/musegadget.sock")
INGRESS_PEERS = {"172.30.32.2", "127.0.0.1", "::1"}   # Home Assistant's ingress proxy (and local tests)
PAIR_HELPER = os.environ.get("MUSE_LINK_PAIR_HELPER", os.path.join(HERE, "pair_helper.py"))
VERSION = "1.2.0"
CAP_NET_ADMIN, CAP_NET_RAW = 12, 13

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("muse-link")


class _QuietUnpaired(logging.Filter):
    """The SDK says "run `musegadget pair`" every 30 seconds until paired; here, pairing is on the setup page."""

    def filter(self, record):
        return not str(record.msg).startswith("not paired;")


logging.getLogger("musegadget.service").addFilter(_QuietUnpaired())


def read_options() -> dict:
    try:
        with open(os.path.join(DATA, "options.json"), encoding="utf-8") as handle:
            return json.load(handle) or {}
    except (OSError, ValueError):
        return {}


OPTIONS = read_options()
TOKEN = str(OPTIONS.get("sdk_token") or "").strip()
if TOKEN:
    os.environ["MUSEGADGET_SDK_TOKEN"] = TOKEN


def _port(env: str, option, default: int) -> int:
    for value in (os.environ.get(env), option):
        try:
            if value not in (None, "") and 0 < int(value) < 65536:
                return int(value)
        except (TypeError, ValueError):
            pass
    return default


# The add-on uses the host's network (Bluetooth sockets only exist there), so
# these are ports on the Home Assistant machine itself.
BRIDGE_PORT = _port("MUSE_LINK_BRIDGE_PORT", OPTIONS.get("bridge_port"), 8791)


def drop_capabilities(*caps: int) -> bool:
    """Give up Linux capabilities for this process and anything it starts.

    The add-on gets network-admin only so pairing can borrow the Bluetooth
    adapter; the pairing broker keeps it, and the rest of Muse Link (the part
    on the network) drops it, along with Docker's default raw-socket one.
    """
    import ctypes

    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return False
    pr_capbset_drop, pr_cap_ambient, pr_cap_ambient_lower = 24, 47, 3
    for cap in caps:
        libc.prctl(pr_cap_ambient, pr_cap_ambient_lower, cap, 0, 0)
        libc.prctl(pr_capbset_drop, cap, 0, 0, 0)  # needs CAP_SETPCAP; harmless if refused

    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class Data(ctypes.Structure):
        _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]

    header, data = Header(0x20080522, 0), (Data * 2)()  # _LINUX_CAPABILITY_VERSION_3
    if libc.capget(ctypes.byref(header), data) != 0:
        return False
    for cap in caps:
        word, mask = cap // 32, ~(1 << (cap % 32)) & 0xFFFFFFFF
        data[word].effective &= mask
        data[word].permitted &= mask
        data[word].inheritable &= mask
    return libc.capset(ctypes.byref(header), data) == 0

sys.path.insert(0, HERE)
import bridge  # noqa: E402  (after the environment is set up)
from musegadget import config, identity  # noqa: E402
from musegadget import service as gadget_service  # noqa: E402
from musegadget.executor import Account  # noqa: E402


# ---------------------------------------------------------------------------
# The wall card's key and chat, made once and kept in /data


WALL_FILE = os.path.join(DATA, "wall.json")


def wall_config() -> dict:
    try:
        with open(WALL_FILE, encoding="utf-8") as handle:
            cfg = json.load(handle)
    except (OSError, ValueError):
        cfg = {}
    changed = False
    if not isinstance(cfg.get("key"), str) or len(cfg["key"]) < 16:
        cfg["key"] = secrets.token_urlsafe(24)
        changed = True
    if not isinstance(cfg.get("chat_id"), str):
        cfg["chat_id"] = str(uuid.uuid4())
        changed = True
    if changed:
        save_wall(cfg)
    return {"key": cfg["key"], "chat_id": cfg["chat_id"], "port": BRIDGE_PORT}


def save_wall(cfg: dict) -> None:
    os.makedirs(DATA, exist_ok=True)
    tmp = WALL_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"key": cfg["key"], "chat_id": cfg["chat_id"]}, handle)
    os.replace(tmp, WALL_FILE)


# ---------------------------------------------------------------------------
# Pairing with the Muse app, through the pairing broker (pair_helper.py --broker)


class Pairing:
    """Talks to the pairing broker, which keeps the Bluetooth permission this process gives up."""

    def __init__(self) -> None:
        self.state = "idle"        # idle | starting | open | paired | closed | failed
        self.name = ""
        self.until = 0.0
        self.problem = ""
        self.hint = ""             # "" | android: the app hung up after confirming, over a large MTU | dropped
        self.borrowed = False      # Home Assistant's Bluetooth adapter is lent to pairing right now
        self.running = False
        self.broker: asyncio.subprocess.Process | None = None

    def snapshot(self) -> dict:
        left = max(0, int(self.until - time.time())) if self.state == "open" else 0
        return {"state": self.state, "name": self.name, "seconds_left": left, "problem": self.problem,
                "hint": self.hint, "borrowed": self.borrowed}

    async def start_broker(self) -> None:
        """Start the broker now, while this process still has the permission it hands down."""
        self.broker = await asyncio.create_subprocess_exec(
            sys.executable, PAIR_HELPER, "--broker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=None, env=dict(os.environ),
        )
        asyncio.ensure_future(self._follow(self.broker))

    async def _send(self, command: str) -> bool:
        broker = self.broker
        if broker is None or broker.returncode is not None or broker.stdin is None:
            return False
        try:
            broker.stdin.write(command.encode() + b"\n")
            await broker.stdin.drain()
            return True
        except (ConnectionError, OSError):
            return False

    async def start(self) -> None:
        if self.running:
            return
        self.state, self.name, self.problem, self.hint = "starting", "", "", ""
        self.running = True
        if not await self._send("start"):
            self.running = False
            self.state = "failed"
            self.problem = "Pairing stopped working. Restart the add-on and try again."

    def cancel(self) -> None:
        if self.running:
            asyncio.ensure_future(self._send("cancel"))

    async def _follow(self, broker) -> None:
        while True:
            line = await broker.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            self._handle(event)
        await broker.wait()
        log.warning("the pairing helper stopped; restart the add-on to pair")
        self.running, self.borrowed = False, False
        if self.state in ("starting", "open"):
            self.state = "closed"

    def _handle(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "open":
            self.state, self.name = "open", str(event.get("name") or "")
            self.until = time.time() + int(event.get("timeout") or 600)
            log.info("pairing open: in the Muse app, add the device named %s", self.name)
        elif kind == "borrowed":
            self.borrowed = True
            log.info("Home Assistant's Bluetooth is paused while pairing is open")
        elif kind == "returned":
            self.borrowed = False
        elif kind == "fallback":
            log.warning("pairing through Home Assistant's Bluetooth instead (%s); Android phones may not finish",
                        event.get("reason"))
        elif kind == "dropped":
            # The Muse Android app writes (MTU - 3)-byte packets and fails above
            # 512 bytes; Home Assistant OS's BlueZ allows an MTU of 517.
            mtu = event.get("mtu") if isinstance(event.get("mtu"), int) else 0
            self.hint = "android" if mtu > 515 else "dropped"
            log.warning("the Muse app disconnected right after confirming pairing (ATT MTU %s)", mtu or "?")
        elif kind == "done":
            outcome = event.get("outcome")
            if outcome in ("paired", "already_paired"):
                self.state = "paired"
                log.info("paired with the Muse; connecting")
            elif outcome == "window_closed":
                self.state = "closed"
            else:
                self.state = "failed"
                self.problem = friendly_problem(event.get("problem") or outcome or "")
                log.warning("pairing failed: %s", event.get("problem") or outcome)
        elif kind == "exit":
            self.running, self.borrowed = False, False
            if self.state in ("starting", "open"):
                self.state = "closed"


def friendly_problem(text: str) -> str:
    low = text.lower()
    if "no bluetooth adapter" in low or "no_adapter" in low:
        return "This Home Assistant machine has no Bluetooth. Plug in a USB Bluetooth adapter, restart the add-on, and try again."
    if "pairing stopped working" in low:
        return text
    if "dbus" in low or "d-bus" in low:
        return "The add-on couldn't reach Bluetooth. Restart the add-on and try again."
    if "sdk token" in low or "invalid_sdk_token" in low:
        return "The SDK token isn't valid. Copy it again from gadgets.muse.ai into the Configuration tab, then restart the add-on."
    return text or "Pairing didn't work. Try again."


# ---------------------------------------------------------------------------
# Moving a pairing from another machine (made there by bridge/move-pairing.sh)


MOVE_BEGIN = "-----BEGIN MUSE PAIRING-----"
MOVE_END = "-----END MUSE PAIRING-----"
MAX_MOVE_CHARS = 32 * 1024


class MoveError(ValueError):
    pass


def read_move_code(text: str) -> tuple[str, dict]:
    """The gadget's MAC and its saved pairing, from a code made on another machine."""
    text = str(text or "")
    if len(text) > MAX_MOVE_CHARS:
        raise MoveError("That's too long to be a pairing code.")
    if MOVE_BEGIN in text:
        text = text.split(MOVE_BEGIN, 1)[1]
    text = text.split(MOVE_END, 1)[0]
    compact = "".join(text.split())
    if not compact:
        raise MoveError("Paste the code first.")
    try:
        bundle = json.loads(base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise MoveError("That isn't a whole pairing code. Copy everything from BEGIN to END and paste it again.")
    if not isinstance(bundle, dict) or bundle.get("v") != 1:
        raise MoveError("That isn't a pairing code from move-pairing.sh.")
    mac = (bundle.get("identity") or {}).get("mac") if isinstance(bundle.get("identity"), dict) else None
    pairing = bundle.get("pairing")
    if not (isinstance(mac, str) and identity._MAC_RE.fullmatch(mac)):
        raise MoveError("The code is missing the device's identity.")
    if not (isinstance(pairing, dict)
            and all(isinstance(pairing.get(k), str) and pairing.get(k) for k in ("access_token", "refresh_token"))
            and all(isinstance(v, (str, int, float, bool)) or v is None for v in pairing.values())):
        raise MoveError("The code is missing the pairing. Make sure that machine is paired with Muse.")
    for key in ("api_url", "api_url_v2"):
        if pairing.get(key) and not str(pairing[key]).startswith("https://"):
            raise MoveError("The pairing in that code doesn't look right.")
    return mac, pairing


def supervisor_get(path: str, method: str = "GET") -> dict:
    """Call Home Assistant's Supervisor API (blocking)."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("not running under the Supervisor")
    req = urllib.request.Request("http://supervisor" + path, method=method,
                                 headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return (json.load(resp) or {}).get("data") or {}


def restart_self() -> None:
    """Start over, so the gadget comes up as the moved identity.

    Under Home Assistant the Supervisor restarts the add-on, which also gives
    the pairing broker back the permission this process gave up; a plain
    re-exec (local runs) would not.
    """
    log.info("restarting Muse Link to use the moved pairing")
    for handler in logging.getLogger().handlers:
        handler.flush()
    if os.environ.get("SUPERVISOR_TOKEN"):
        try:
            supervisor_get("/addons/self/restart", "POST")
            return
        except Exception as exc:
            log.warning("couldn't ask Home Assistant to restart the add-on (%s); restarting in place", exc)
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ---------------------------------------------------------------------------
# The setup page (Home Assistant's sidebar, through ingress)


class SetupPage:
    def __init__(self, wall: bridge.Wall, pairing: Pairing, device: str) -> None:
        self.wall = wall
        self.pairing = pairing
        self.device = device
        self.moving = False
        self.host_port = BRIDGE_PORT
        self.host_ip = ""
        with open(os.path.join(HERE, "setup.html"), "rb") as handle:
            self.page = handle.read()

    def state(self) -> dict:
        paired = bool(config.load_json(config.PAIRING_FILE))
        token_problem = ""
        if not TOKEN:
            token_problem = "missing"
        else:
            try:
                config.sdk_token()
            except ValueError:
                token_problem = "invalid"
        if paired and self.pairing.state in ("idle", "closed", "failed"):
            self.pairing.state = "idle"
        return {
            "version": VERSION, "token": token_problem or "ok", "paired": paired,
            "connected": self.wall.session() is not None, "pairing": self.pairing.snapshot(),
            "device": self.device, "moving": self.moving,
            "key": self.wall.key, "port": self.host_port, "ip": self.host_ip,
        }

    async def move_here(self, body: bytes) -> tuple[int, dict]:
        """Save a pairing made on another machine, then restart as that gadget."""
        if self.moving:
            return 409, {"error": "Already moving a pairing. Wait a moment."}
        try:
            code = (json.loads(body or b"{}") or {}).get("code")
        except (ValueError, AttributeError):
            code = None
        try:
            mac, record = read_move_code(code if isinstance(code, str) else "")
        except MoveError as exc:
            return 400, {"error": str(exc)}
        self.pairing.cancel()
        config.save_json(config.IDENTITY_FILE, {"mac": mac})
        config.save_json(config.PAIRING_FILE, record)
        self.moving = True
        moved = identity.Identity(mac).ble_name
        log.info("moved a pairing here from another machine: this add-on is now %s", moved)
        asyncio.get_running_loop().call_later(1.0, lambda: asyncio.ensure_future(asyncio.to_thread(restart_self)))
        return 200, dict(self.state(), device=moved)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("",))[0]
        try:
            if peer not in INGRESS_PEERS:
                return await bridge.respond(writer, 401, {"error": "Open Muse Link from Home Assistant's sidebar."})
            try:
                method, target, _headers, body = await asyncio.wait_for(bridge.read_request(reader), 15)
            except Exception:
                return
            path = target.split("?", 1)[0].rstrip("/") or "/"
            if method == "GET" and path == "/":
                writer.write(bridge.head(200, "text/html; charset=utf-8", len(self.page)) + self.page)
                await writer.drain()
            elif method == "GET" and path == "/api/state":
                await bridge.respond(writer, 200, self.state())
            elif method == "POST" and path == "/api/pair":
                if not TOKEN:
                    return await bridge.respond(writer, 400, {"error": "Add your SDK token in the Configuration tab first."})
                await self.pairing.start()
                await bridge.respond(writer, 200, self.state())
            elif method == "POST" and path == "/api/pair/cancel":
                self.pairing.cancel()
                await bridge.respond(writer, 200, self.state())
            elif method == "POST" and path == "/api/new-key":
                self.wall.key = secrets.token_urlsafe(24)
                save_wall({"key": self.wall.key, "chat_id": self.wall.chat_id})
                log.info("made a new wall key; the HomeGlow card needs the new one")
                await bridge.respond(writer, 200, self.state())
            elif method == "POST" and path == "/api/move":
                status, data = await self.move_here(body)
                await bridge.respond(writer, status, data)
            else:
                await bridge.respond(writer, 404, {"error": "Not found."})
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def learn_network(self) -> None:
        """Ask the Supervisor for this machine's address on the home network."""
        try:
            net = await asyncio.to_thread(supervisor_get, "/network/info")
            for iface in net.get("interfaces") or []:
                if iface.get("primary") and (iface.get("ipv4") or {}).get("address"):
                    self.host_ip = iface["ipv4"]["address"][0].split("/")[0]
                    break
        except Exception as exc:
            log.info("couldn't read this machine's address from the Supervisor: %s", exc)


# ---------------------------------------------------------------------------


async def main() -> None:
    for folder in (config.state_dir(), os.path.dirname(str(config.socket_path()))):
        os.makedirs(folder, mode=0o700, exist_ok=True)
    cfg = wall_config()

    # Muse may only run the health check here.
    gadget_service.COMMAND_SPECS = {"device.health": gadget_service.COMMAND_SPECS["device.health"]}
    executor = bridge.WallOnlyExecutor(Account.current())
    ident = identity.load_or_create()
    service = gadget_service.Service(identity=ident, executor=executor, sdk_token=TOKEN or None)
    pairing = Pairing()

    # The broker keeps network-admin (for borrowing the Bluetooth adapter);
    # everything else here, which talks to the network and to Muse, drops it.
    await pairing.start_broker()
    if drop_capabilities(CAP_NET_ADMIN, CAP_NET_RAW):
        log.info("dropped network-admin; only the pairing helper keeps it")
    else:
        log.warning("couldn't drop network-admin from the main process")

    ui_port, ui_host = _port("MUSE_LINK_UI_PORT", None, 0), "0.0.0.0"
    try:
        info = await asyncio.to_thread(supervisor_get, "/addons/self/info")
        ui_port = ui_port or _port("", info.get("ingress_port"), 0)
        ui_host = str(info.get("ip_address") or ui_host)
    except Exception as exc:
        log.info("couldn't read the ingress address from the Supervisor (%s); using defaults", exc)
    ui_port = ui_port or 8099

    loop = asyncio.get_running_loop()

    def stop() -> None:
        pairing.cancel()
        service.stop()

    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop)

    local = await service.serve_local(config.socket_path())
    wall = bridge.Wall(service, cfg)
    setup = SetupPage(wall, pairing, ident.ble_name)
    web = await asyncio.start_server(wall.handle, "0.0.0.0", BRIDGE_PORT, limit=64 * 1024)
    try:
        # Only Home Assistant's own network: the setup page isn't for the rest of the house.
        ui = await asyncio.start_server(setup.handle, ui_host, ui_port, limit=64 * 1024)
    except OSError as exc:
        log.warning("couldn't listen on %s:%d (%s); listening on all addresses", ui_host, ui_port, exc)
        ui = await asyncio.start_server(setup.handle, "0.0.0.0", ui_port, limit=64 * 1024)
    asyncio.ensure_future(setup.learn_network())
    tasks = [asyncio.ensure_future(wall.subscribe_forever(service._stop)),
             asyncio.ensure_future(wall.tick_forever(service._stop))]

    log.info("Muse Link %s started. Open Muse Link in Home Assistant's sidebar to pair and to get the wall card's address and key.", VERSION)
    if not TOKEN:
        log.warning("No SDK token yet: add it in the add-on's Configuration tab, then restart the add-on.")
    elif not config.load_json(config.PAIRING_FILE):
        log.info("Not paired yet: open Muse Link in the sidebar to pair, or to move a pairing from another machine.")
    else:
        log.info("Paired as %s.", ident.ble_name)
    try:
        await service.run()
    finally:
        for task in tasks:
            task.cancel()
        for server in (web, ui, local):
            server.close()


if __name__ == "__main__":
    asyncio.run(main())
