#!/usr/bin/env python3
"""Muse Link: a Home Assistant add-on that connects a Muse to the HomeGlow wall card.

One process runs three things:
  * Meta's Muse gadget service, which keeps this add-on connected to your Muse;
  * the wall bridge (bridge.py) on port 8791, which the HomeGlow card talks to;
  * the setup page, shown in Home Assistant's sidebar through ingress, for
    pairing with the Muse app and copying the wall card's address and key.

Muse cannot run commands or change files here: the add-on answers its health
check and refuses everything else.
"""

from __future__ import annotations

import asyncio
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
BRIDGE_PORT = int(os.environ.get("MUSE_LINK_BRIDGE_PORT", "8791"))
UI_PORT = int(os.environ.get("MUSE_LINK_UI_PORT", "8099"))
INGRESS_PEERS = {"172.30.32.2", "127.0.0.1", "::1"}   # Home Assistant's ingress proxy (and local tests)
PAIR_HELPER = os.environ.get("MUSE_LINK_PAIR_HELPER", os.path.join(HERE, "pair_helper.py"))
VERSION = "1.0.0"

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
# Pairing with the Muse app, in its own process


class Pairing:
    def __init__(self) -> None:
        self.state = "idle"        # idle | starting | open | paired | closed | failed
        self.name = ""
        self.until = 0.0
        self.problem = ""
        self.proc: asyncio.subprocess.Process | None = None

    def snapshot(self) -> dict:
        left = max(0, int(self.until - time.time())) if self.state == "open" else 0
        return {"state": self.state, "name": self.name, "seconds_left": left, "problem": self.problem}

    async def start(self) -> None:
        if self.proc is not None:
            return
        self.state, self.name, self.problem = "starting", "", ""
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, PAIR_HELPER,
            stdout=asyncio.subprocess.PIPE, stderr=None, env=dict(os.environ),
        )
        asyncio.ensure_future(self._follow(self.proc))

    async def _follow(self, proc) -> None:
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "open":
                    self.state, self.name = "open", str(event.get("name") or "")
                    self.until = time.time() + int(event.get("timeout") or 600)
                    log.info("pairing open: in the Muse app, add the device named %s", self.name)
                elif event.get("event") == "done":
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
            await proc.wait()
            if self.state in ("starting", "open"):
                self.state = "closed"
        finally:
            self.proc = None

    def cancel(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()


def friendly_problem(text: str) -> str:
    low = text.lower()
    if "no bluetooth adapter" in low:
        return "This Home Assistant machine has no Bluetooth. Plug in a USB Bluetooth adapter, restart the add-on, and try again."
    if "dbus" in low or "d-bus" in low:
        return "The add-on couldn't reach Bluetooth. Restart the add-on and try again."
    if "sdk token" in low or "invalid_sdk_token" in low:
        return "The SDK token isn't valid. Copy it again from gadgets.muse.ai into the Configuration tab, then restart the add-on."
    return text or "Pairing didn't work. Try again."


# ---------------------------------------------------------------------------
# The setup page (Home Assistant's sidebar, through ingress)


class SetupPage:
    def __init__(self, wall: bridge.Wall, pairing: Pairing) -> None:
        self.wall = wall
        self.pairing = pairing
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
            "key": self.wall.key, "port": self.host_port, "ip": self.host_ip,
        }

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("",))[0]
        try:
            if peer not in INGRESS_PEERS:
                return await bridge.respond(writer, 401, {"error": "Open Muse Link from Home Assistant's sidebar."})
            try:
                method, target, _headers, _body = await asyncio.wait_for(bridge.read_request(reader), 15)
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
        """Ask the Supervisor which port Home Assistant gave the card, and this machine's address."""
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return

        def get(path):
            req = urllib.request.Request("http://supervisor" + path, headers={"Authorization": "Bearer " + token})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.load(resp).get("data") or {}

        try:
            info = await asyncio.to_thread(get, "/addons/self/info")
            port = (info.get("network") or {}).get("%d/tcp" % BRIDGE_PORT)
            if isinstance(port, int):
                self.host_port = port
        except Exception as exc:
            log.info("couldn't read the add-on's port from the Supervisor: %s", exc)
        try:
            net = await asyncio.to_thread(get, "/network/info")
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
    service = gadget_service.Service(identity=identity.load_or_create(), executor=executor, sdk_token=TOKEN or None)
    pairing = Pairing()

    loop = asyncio.get_running_loop()

    def stop() -> None:
        pairing.cancel()
        service.stop()

    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop)

    local = await service.serve_local(config.socket_path())
    wall = bridge.Wall(service, cfg)
    setup = SetupPage(wall, pairing)
    web = await asyncio.start_server(wall.handle, "0.0.0.0", BRIDGE_PORT, limit=64 * 1024)
    ui = await asyncio.start_server(setup.handle, "0.0.0.0", UI_PORT, limit=64 * 1024)
    asyncio.ensure_future(setup.learn_network())
    tasks = [asyncio.ensure_future(wall.subscribe_forever(service._stop)),
             asyncio.ensure_future(wall.tick_forever(service._stop))]

    log.info("Muse Link %s started. Open Muse Link in Home Assistant's sidebar to pair and to get the wall card's address and key.", VERSION)
    if not TOKEN:
        log.warning("No SDK token yet: add it in the add-on's Configuration tab, then restart the add-on.")
    elif not config.load_json(config.PAIRING_FILE):
        log.info("Not paired yet: open Muse Link in the sidebar and click Start pairing.")
    try:
        await service.run()
    finally:
        for task in tasks:
            task.cancel()
        for server in (web, ui, local):
            server.close()


if __name__ == "__main__":
    asyncio.run(main())
