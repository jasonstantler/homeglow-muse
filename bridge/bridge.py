#!/usr/bin/env python3
"""HomeGlow wall bridge for Muse.

Runs the Muse gadget service from Meta's Linux Device SDK and, beside it, a
small web server for the HomeGlow Muse card. What you type or say on the wall
goes to a "Wall" side chat in your Muse; Muse's progress, answers and voice
come back to the card.

systemd starts this in place of `musegadget run` (see install-bridge.sh).
Settings live in /etc/homeglow-muse/config.json:

  port            the web server's port (default 8791)
  key             the wall key the card must send; keep it secret
  chat_id         the Wall side chat in Muse (made up once at install)
  allow_commands  false (default): this device only links the wall to Muse,
                  and Muse cannot run commands or touch files on it.
                  true: Muse gets the SDK's usual shell and file commands.
  muse_voice      false (default): Muse answers in writing and the card reads
                  it out with the browser's voice. true: ask Muse for spoken
                  answers in its own voice. (In October 2026 Muse failed those
                  with "I ran into a problem", so leave this off until it works.)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import signal
import sys
import time
import uuid
from collections import OrderedDict
from urllib.parse import parse_qs, urlsplit

from musegadget import __version__ as GADGET_VERSION
from musegadget import config, identity
from musegadget import service as gadget_service
from musegadget.executor import Account, Executor, error as command_error
from musegadget.muse_turn import AgentStatus, MuseTurn, ReplayScope, Reply, _acknowledged_message_ids

BRIDGE_VERSION = "1.1.0"
log = logging.getLogger("homeglow-muse")

CONFIG_PATH = os.environ.get("HOMEGLOW_MUSE_CONFIG", "/etc/homeglow-muse/config.json")
MAX_TEXT_CHARS = 2000
MAX_WAV_BYTES = 2 * 1024 * 1024          # the SDK's limit for a voice note (about a minute)
MAX_BODY_BYTES = MAX_WAV_BYTES + 64 * 1024
TURN_LIMIT_S = 300                       # stop following a request after 5 minutes
ACK_LIMIT_S = 75                         # Muse must accept a request within this
KEEP_TURNS = 20
ACTIVE = ("sending", "working", "answering")
FRIENDLY = {
    "thinking": "Thinking", "researching": "Researching", "planning": "Planning",
    "working": "Working on it", "executing": "Working on it", "processing": "Working on it",
    "generating": "Writing", "responding": "Answering", "listening": "Listening", "speaking": "Speaking",
}
_TURN_ID = re.compile(r"[a-f0-9]{12}")
_MESSAGE_ID = re.compile(r"[A-Za-z0-9._:-]{1,200}")
_CHAT_ID = re.compile(r"[A-Za-z0-9-]{1,64}")


class WallError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class WallOnlyExecutor(Executor):
    """Answers Muse's health check and refuses everything else."""

    def run(self, command: str, params: dict, timeout_ms: int | None = None) -> dict:
        if command == "device.health":
            return super().run(command, params, timeout_ms)
        log.warning("refused %s: this device only links the wall to Muse", command)
        return command_error("This device only links the HomeGlow wall to Muse. It does not run commands or change files.")


# ---------------------------------------------------------------------------
# One request from the wall


class WallTurn:
    def __init__(self, chat_id: str, scope: ReplayScope, speak: bool, voice: bool):
        self.id = uuid.uuid4().hex[:12]
        self.created = time.monotonic()
        self.speak = speak
        self.voice = voice
        self.state = "sending"
        self.activity = ""
        self.error = ""
        self.note = ""
        self.user_ids: set[str] = set()
        self.turn = MuseTurn(chat_id, owns_chat=True, replay_scope=scope)

    def snapshot(self, heard: str) -> dict:
        replies = [{"id": r.message_id, "text": r.text, "done": bool(r.done or self.state == "done")}
                   for r in self.turn.replies.values() if r.text.strip()]
        return {
            "id": self.id, "state": self.state, "activity": self.activity, "error": self.error,
            "note": self.note, "heard": heard, "speak": self.speak, "replies": replies,
        }


# ---------------------------------------------------------------------------
# The bridge between the wall and Muse


class Wall:
    def __init__(self, service, cfg: dict):
        self.service = service
        self.key = cfg["key"]
        self.muse_voice = cfg.get("muse_voice") is True
        self.chat_id = cfg["chat_id"]
        self.scope = ReplayScope()
        self.turns: "OrderedDict[str, WallTurn]" = OrderedDict()
        self.current: WallTurn | None = None
        self.user_text: "OrderedDict[str, str]" = OrderedDict()

    # -- Muse side -----------------------------------------------------------

    def session(self):
        session = getattr(self.service, "_current", None)
        return session if session is not None and session.registered_at is not None else None

    async def subscribe_forever(self, stop: asyncio.Event) -> None:
        """Follow the Wall chat for as long as Muse is connected."""
        while not stop.is_set():
            session = self.session()
            if session is None:
                await asyncio.sleep(1)
                continue
            try:
                async for event in session.subscribe_chat(self.chat_id):
                    self.on_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the session dropped; the service reconnects it
                log.info("chat subscription ended: %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(2)

    def on_event(self, event: dict) -> None:
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            return
        if payload.get("role") == "user":
            self._remember_user_text(event, payload)
        current = self.current
        if current is not None and current.state in ACTIVE:
            try:
                self.apply(current, current.turn.feed(event))
            except ValueError as exc:
                log.warning("dropping a reply: %s", exc)
                self.fail(current, "Muse's answer was too long to show here. It's in the Muse app.")
        else:
            self.scope.observe_idle(event, self.chat_id)

    def _remember_user_text(self, event: dict, payload: dict) -> None:
        message_id = payload.get("message_id") or event.get("message_id") or payload.get("id")
        if not isinstance(message_id, str):
            return
        for key in ("display_text", "transcript", "text", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                self.user_text[message_id] = value.strip()[:500]
                while len(self.user_text) > 50:
                    self.user_text.popitem(last=False)
                return

    def apply(self, turn: WallTurn, events) -> None:
        for event in events:
            if isinstance(event, AgentStatus):
                text = (event.activity_text or FRIENDLY.get(event.activity_code or "", "")).strip()
                if text:
                    turn.activity = text[:120]
            elif isinstance(event, Reply) and event.text.strip() and turn.state in ("sending", "working"):
                turn.state = "answering"

    def fail(self, turn: WallTurn, message: str) -> None:
        turn.state = "error"
        turn.error = message
        turn.turn.retire()
        log.info("request %s failed: %s", turn.id, message)

    def finish(self, turn: WallTurn, note: str = "") -> None:
        turn.state = "done"
        turn.note = note
        turn.turn.retire()

    async def tick_forever(self, stop: asyncio.Event) -> None:
        """Decide when Muse has finished answering."""
        while not stop.is_set():
            await asyncio.sleep(0.4)
            turn = self.current
            if turn is None or turn.state not in ACTIVE:
                continue
            now = time.monotonic()
            if turn.state == "sending":
                if now - turn.created > ACK_LIMIT_S:
                    self.fail(turn, "Muse didn't pick up the request. Try again.")
                continue
            if turn.turn.complete(now) or turn.turn.settled(now):
                self.finish(turn)
            elif now - turn.created > TURN_LIMIT_S:
                self.finish(turn, "Muse is still working on this. The rest will be in the Muse app.")

    async def ask(self, text: str | None = None, wav: bytes | None = None, speak: bool = False) -> WallTurn:
        session = self.session()
        if session is None:
            raise WallError(503, "Muse isn't connected right now. Check the bridge on the VM.")
        if self.current is not None and self.current.state in ACTIVE:
            raise WallError(409, "Muse is still answering the last request.")
        turn = WallTurn(self.chat_id, self.scope, speak and self.muse_voice, wav is not None)
        self.current = turn
        self.turns[turn.id] = turn
        while len(self.turns) > KEEP_TURNS:
            self.turns.popitem(last=False)
        asyncio.ensure_future(self._send(session, turn, text, wav))
        return turn

    async def _send(self, session, turn: WallTurn, text: str | None, wav: bytes | None) -> None:
        try:
            await asyncio.wait_for(session.chat_subscribed.wait(), 5)
        except asyncio.TimeoutError:
            log.warning("sending before the Wall chat is being followed; early progress may be missed")
        modality = "voice" if turn.speak else "text"
        try:
            if wav is not None:
                response = await session.send_voice(wav, self.chat_id, output_modality=modality)
            else:
                response = await session.send_chat(text or "", self.chat_id, output_modality=modality)
        except Exception as exc:
            log.warning("send failed: %s: %s", type(exc).__name__, exc)
            return self.fail(turn, "Couldn't reach Muse. Try again in a moment.")
        if turn.state != "sending":
            return
        if not response.get("ok"):
            return self.fail(turn, "Muse turned the request down (error %s)." % response.get("status"))
        body = response.get("response")
        if not isinstance(body, dict):
            return self.fail(turn, "Muse sent back something unexpected.")
        try:
            turn.user_ids = _acknowledged_message_ids(body)
            held = turn.turn.acknowledge(body)
        except ValueError as exc:
            # Follow the reply through its links instead of owning the chat.
            log.warning("Muse did not confirm the Wall chat (%s); following the reply by its links", exc)
            turn.turn = MuseTurn(self.chat_id)
            try:
                held = turn.turn.acknowledge(body)
            except ValueError as again:
                log.warning("acknowledgement unusable: %s", again)
                return self.fail(turn, "Muse sent back something unexpected.")
        turn.state = "working"
        self.apply(turn, held)

    def heard(self, turn: WallTurn) -> str:
        if not turn.voice:
            return ""
        for message_id in turn.user_ids:
            if message_id in self.user_text:
                return self.user_text[message_id]
        return ""

    # -- Wall side -----------------------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                method, target, headers, body = await asyncio.wait_for(read_request(reader), 30)
            except WallError as exc:
                return await respond(writer, exc.status, {"error": exc.message})
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
                return
            url = urlsplit(target)
            query = parse_qs(url.query)
            path = url.path.rstrip("/") or "/"
            if method == "OPTIONS":
                return await respond(writer, 204, None)
            key = headers.get("x-wall-key") or (query.get("key") or [""])[0]
            if not hmac.compare_digest(key.encode(), self.key.encode()):
                await asyncio.sleep(1)
                return await respond(writer, 401, {"error": "Wrong wall key. Copy it again from the bridge setup."})
            await self.route(writer, method, path, query, headers, body)
        except WallError as exc:
            await respond(writer, exc.status, {"error": exc.message})
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("request failed")
            try:
                await respond(writer, 500, {"error": "The bridge hit a problem. Its log has details."})
            except Exception:
                pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def route(self, writer, method, path, query, headers, body) -> None:
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/status":
            busy = self.current is not None and self.current.state in ACTIVE
            return await respond(writer, 200, {"connected": self.session() is not None, "busy": busy,
                                               "version": BRIDGE_VERSION, "gadget": GADGET_VERSION})
        if method == "POST" and path == "/ask":
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                raise WallError(400, "The request wasn't readable.")
            text = data.get("text") if isinstance(data, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise WallError(400, "Type something to send.")
            if len(text) > MAX_TEXT_CHARS:
                raise WallError(400, "That's too long. Keep it under %d characters." % MAX_TEXT_CHARS)
            turn = await self.ask(text=text.strip(), speak=data.get("speak") is True)
            return await respond(writer, 200, {"turn": turn.snapshot("")})
        if method == "POST" and path == "/ask-voice":
            if len(body) > MAX_WAV_BYTES:
                raise WallError(413, "That recording is too long. Keep it under a minute.")
            if len(body) < 44 or body[:4] != b"RIFF" or body[8:12] != b"WAVE":
                raise WallError(400, "The recording wasn't a WAV file.")
            speak = (query.get("speak") or ["0"])[0] == "1"
            turn = await self.ask(wav=body, speak=speak)
            return await respond(writer, 200, {"turn": turn.snapshot("")})
        if method == "GET" and len(parts) == 2 and parts[0] == "turn" and _TURN_ID.fullmatch(parts[1]):
            turn = self.turns.get(parts[1])
            if turn is None:
                raise WallError(404, "That request is no longer here.")
            return await respond(writer, 200, {"turn": turn.snapshot(self.heard(turn))})
        if method == "GET" and len(parts) == 3 and parts[0] == "speech" and _TURN_ID.fullmatch(parts[1]) \
                and _MESSAGE_ID.fullmatch(parts[2]):
            return await self.speech(writer, parts[1], parts[2])
        raise WallError(404, "Not found.")

    async def speech(self, writer, turn_id: str, message_id: str) -> None:
        """Muse's own voice for one answer, as MP3, passed on as it arrives."""
        turn = self.turns.get(turn_id)
        reply = turn.turn.replies.get(message_id) if turn else None
        if turn is None or reply is None or not turn.speak:
            raise WallError(404, "No voice for that answer.")
        session = self.session()
        if session is None:
            raise WallError(503, "Muse isn't connected right now.")
        chunks = session.stream_tts(message_id)
        try:
            try:
                first = await asyncio.wait_for(chunks.__anext__(), 30)
            except StopAsyncIteration:
                raise WallError(502, "Muse sent no voice for that answer.")
            except WallError:
                raise
            except Exception as exc:
                log.info("voice unavailable for %s: %s", message_id, exc)
                raise WallError(502, "Muse's voice isn't available for that answer.")
            writer.write(head(200, "audio/mpeg", chunked=True))
            writer.write(chunk(first))
            await writer.drain()
            async for part in chunks:
                writer.write(chunk(part))
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        finally:
            await chunks.aclose()


# ---------------------------------------------------------------------------
# A very small HTTP/1.1 server: one request per connection


CORS = (
    "Access-Control-Allow-Origin: *\r\n"
    "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
    "Access-Control-Allow-Headers: Content-Type, X-Wall-Key\r\n"
    "Access-Control-Allow-Private-Network: true\r\n"
    "Access-Control-Max-Age: 600\r\n"
)
REASONS = {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
           409: "Conflict", 413: "Payload Too Large", 500: "Internal Server Error", 502: "Bad Gateway",
           503: "Service Unavailable"}


async def read_request(reader: asyncio.StreamReader):
    line = await reader.readline()
    if not line:
        raise ConnectionError("closed")
    try:
        method, target, _ = line.decode("latin-1").strip().split(" ", 2)
    except ValueError:
        raise WallError(400, "Bad request.")
    headers = {}
    for _ in range(100):
        raw = await reader.readline()
        if raw in (b"\r\n", b"\n", b""):
            break
        name, _, value = raw.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    else:
        raise WallError(400, "Too many headers.")
    try:
        length = int(headers.get("content-length") or 0)
    except ValueError:
        raise WallError(400, "Bad length.")
    if length < 0 or length > MAX_BODY_BYTES:
        raise WallError(413, "That's too big to send.")
    body = await reader.readexactly(length) if length else b""
    return method.upper(), target, headers, body


def head(status: int, content_type: str | None, length: int | None = None, chunked: bool = False) -> bytes:
    lines = "HTTP/1.1 %d %s\r\n%sCache-Control: no-store\r\nConnection: close\r\n" % (
        status, REASONS.get(status, "OK"), CORS)
    if content_type:
        lines += "Content-Type: %s\r\n" % content_type
    if chunked:
        lines += "Transfer-Encoding: chunked\r\n"
    elif length is not None:
        lines += "Content-Length: %d\r\n" % length
    return (lines + "\r\n").encode("latin-1")


def chunk(data: bytes) -> bytes:
    return b"%x\r\n%s\r\n" % (len(data), data) if data else b""


async def respond(writer: asyncio.StreamWriter, status: int, data) -> None:
    if data is None:
        writer.write(head(status, None, 0))
    else:
        body = json.dumps(data).encode()
        writer.write(head(status, "application/json", len(body)) + body)
    await writer.drain()


# ---------------------------------------------------------------------------
# Start-up


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as handle:
        cfg = json.load(handle)
    if not isinstance(cfg.get("key"), str) or len(cfg["key"]) < 16:
        raise SystemExit("%s: key must be at least 16 characters" % path)
    if not isinstance(cfg.get("chat_id"), str) or not _CHAT_ID.fullmatch(cfg["chat_id"]):
        raise SystemExit("%s: chat_id must be letters, digits and dashes" % path)
    cfg["port"] = int(cfg.get("port") or 8791)
    cfg["host"] = str(cfg.get("host") or "0.0.0.0")
    cfg["allow_commands"] = cfg.get("allow_commands") is True
    cfg["muse_voice"] = cfg.get("muse_voice") is True
    return cfg


async def serve(cfg: dict, executor: Executor, sdk_token: str | None) -> None:
    service = gadget_service.Service(identity=identity.load_or_create(), executor=executor, sdk_token=sdk_token)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, service.stop)
    local = await service.serve_local(config.socket_path())   # keeps `musegadget send-user-msg` working
    wall = Wall(service, cfg)
    web = await asyncio.start_server(wall.handle, cfg["host"], cfg["port"], limit=64 * 1024)
    log.info("HomeGlow wall bridge %s listening on port %d", BRIDGE_VERSION, cfg["port"])
    stop = service._stop
    tasks = [asyncio.ensure_future(wall.subscribe_forever(stop)), asyncio.ensure_future(wall.tick_forever(stop))]
    try:
        await service.run()
    finally:
        for task in tasks:
            task.cancel()
        web.close()
        local.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    run_as = os.environ.get("MUSEGADGET_RUN_AS")
    if os.geteuid() == 0 and not run_as:
        print("Set MUSEGADGET_RUN_AS (the installer does this).", file=sys.stderr)
        return 1
    account = Account.lookup(run_as) if run_as else Account.current()
    if cfg["allow_commands"]:
        executor = Executor(account)
        log.info("Muse may run commands on this device as %s", account.name)
    else:
        executor = WallOnlyExecutor(account)
        # Tell Muse about the health check only, so it doesn't offer to run commands here.
        gadget_service.COMMAND_SPECS = {"device.health": gadget_service.COMMAND_SPECS["device.health"]}
        log.info("wall-only mode: Muse cannot run commands or change files on this device")
    try:
        sdk_token = config.sdk_token()
    except ValueError as exc:
        log.warning("running without an SDK token: %s", exc)
        sdk_token = None
    asyncio.run(serve(cfg, executor, sdk_token))
    return 0


if __name__ == "__main__":
    sys.exit(main())
