"""REST + WebSocket API - dependency-free, asyncio only.

One port serves both:

  REST (JSON)
    GET  /health                     liveness
    GET  /api/status                 OpenClaw state, cycle, uptime, RAM
    GET  /api/config                 the running configuration
    GET  /api/strategies             every strategy in the registry
    GET  /api/strategies/{id}        one strategy + its live history
    GET  /api/cycles                 research cycle audit trail
    GET  /api/live                   live strategies + prop-firm account state
    GET  /api/events?limit=200       recent structural events from the DB
    GET  /api/features/latest        the newest research row
    GET  /api/engine                 engine statistics (ticks, voids, lines)
    POST /api/cycle                  run a research cycle now
    POST /api/strategies/{id}/disable
    POST /api/strategies/{id}/deploy

  WebSocket
    /ws?topics=structural,feature,live,control,log[,tick]
        Live push of everything above. `tick` is opt-in and sampled
        (`api.tick_sample`), because a raw tick firehose would swamp a browser.

Why hand-rolled instead of aiohttp/FastAPI: the deliverable is a single frozen
.exe that must run on a locked-down Windows box with no pip install. The whole
HTTP/1.1 + RFC-6455 surface we need is ~200 lines of stdlib, versus tens of MB
of dependencies in the bundle. It is not a general-purpose web server - it is
exactly this platform's control plane, and it binds to localhost by default.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import time
import urllib.parse
from typing import Any, Callable

from . import logging_setup
from .events import (EventBus, TICK, STRUCTURAL, LINE_BREAK, FEATURE, LIVE,
                     CONTROL, to_dict)
from .logging_setup import get_logger

log = get_logger("api")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_JSON = "application/json; charset=utf-8"


# ------------------------------------------------------------------ framing
def _ws_accept(key: str) -> str:
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _ws_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Server -> client frame (never masked, per RFC 6455)."""
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < (1 << 16):
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + payload


async def _ws_read(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    """Client -> server frame (always masked). Returns (opcode, payload)."""
    head = await reader.readexactly(2)
    fin_op, mask_len = head[0], head[1]
    opcode = fin_op & 0x0F
    masked = bool(mask_len & 0x80)
    n = mask_len & 0x7F
    if n == 126:
        n = struct.unpack(">H", await reader.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", await reader.readexactly(8))[0]
    if n > 2_000_000:                      # a control-plane client sends tiny frames
        raise ValueError("oversized websocket frame")
    mask = await reader.readexactly(4) if masked else b""
    data = await reader.readexactly(n) if n else b""
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return opcode, data


class ApiServer:
    """The platform's control plane."""

    def __init__(self, cfg, bus: EventBus, openclaw=None, store=None,
                 ingest=None, logger=None):
        self.cfg = cfg
        self.bus = bus
        self.openclaw = openclaw
        self.store = store
        self.ingest = ingest
        self.log = logger or (lambda m: None)

        api = cfg.section("api")
        self.host = str(api.get("host", "127.0.0.1"))
        self.port = int(api.get("port", 8765))
        self.tick_sample = max(1, int(api.get("tick_sample", 50)))
        self.enabled = bool(api.get("enabled", True))

        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.Queue] = set()
        self._tick_n = 0
        self._log_off: Callable[[], None] | None = None
        self.requests = 0

    # ------------------------------------------------------------------ run
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        asyncio.create_task(self._pump())
        self._log_off = logging_setup.subscribe(
            lambda rec: self._fanout("log", rec))
        self.log(f"[API] REST + WebSocket listening on http://{self.host}:{self.port}")
        log.info("api started", extra={"host": self.host, "port": self.port})

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._log_off:
            self._log_off()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        log.info("api stopped")

    # ------------------------------------------------------- bus -> clients
    async def _pump(self) -> None:
        """Bridge the event bus into every connected websocket."""
        sub = self.bus.subscribe(STRUCTURAL, LINE_BREAK, FEATURE, LIVE, CONTROL, TICK)
        try:
            while True:
                topic, evt = await sub.get()
                if not self._clients:
                    continue
                if topic == TICK:
                    self._tick_n += 1
                    if self._tick_n % self.tick_sample:
                        continue
                if topic == FEATURE:
                    payload = {"ts": evt.ts, "price": evt.price,
                               "values": {k: round(float(v), 6)
                                          for k, v in evt.values.items()}}
                else:
                    payload = to_dict(evt)
                self._fanout(topic, payload)
        except asyncio.CancelledError:
            pass
        finally:
            sub.close()

    def _fanout(self, topic: str, payload: dict) -> None:
        if not self._clients:
            return
        msg = json.dumps({"topic": topic, "data": payload}, default=str)
        for q in list(self._clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # Slow client: drop this message rather than stall the platform.
                pass

    # -------------------------------------------------------------- routing
    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=15)
            if not line:
                return
            parts = line.decode("latin-1").strip().split()
            if len(parts) < 2:
                return
            method, raw_path = parts[0], parts[1]

            headers: dict[str, str] = {}
            while True:
                hl = await asyncio.wait_for(reader.readline(), timeout=15)
                if hl in (b"\r\n", b"\n", b""):
                    break
                k, _, v = hl.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()

            parsed = urllib.parse.urlparse(raw_path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            self.requests += 1

            if path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
                await self._websocket(reader, writer, headers, query)
                return

            body = b""
            n = int(headers.get("content-length", 0) or 0)
            if n:
                body = await reader.readexactly(min(n, 1_000_000))

            status, payload = await self._route(method, path, query, body)
            await self._respond(writer, status, payload)
        except (asyncio.IncompleteReadError, ConnectionResetError,
                asyncio.TimeoutError, BrokenPipeError):
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("request failed", extra={"err": str(exc)})
            try:
                await self._respond(writer, 500, {"error": str(exc)})
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _respond(self, writer: asyncio.StreamWriter, status: int,
                       payload: Any) -> None:
        body = json.dumps(payload, default=str, indent=2).encode("utf-8")
        reason = {200: "OK", 202: "Accepted", 400: "Bad Request",
                  404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {_JSON}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + body)
        await writer.drain()

    async def _route(self, method: str, path: str, query: dict,
                     body: bytes) -> tuple[int, Any]:
        oc, store = self.openclaw, self.store

        if path == "/health":
            return 200, {"ok": True, "ts": time.time()}

        if path == "/api/status":
            return 200, (oc.status() if oc else {"state": "no controller"})

        if path == "/api/config":
            return 200, self.cfg.data

        if path == "/api/engine":
            if self.ingest is None:
                return 200, {"running": False}
            return 200, {
                "running": True,
                "stats": self.ingest.stats.as_dict(),
                "nonprint": self.ingest.nonprint.stats(),
                "linebreak": self.ingest.linebreak.stats(),
                "database": store.summary() if store else {},
            }

        if path == "/api/live":
            if oc and oc.live:
                return 200, oc.live.status()
            return 200, {"running": False, "live_strategies": []}

        if path == "/api/cycles":
            return 200, (oc.registry.cycles() if oc else [])

        if path == "/api/features/latest":
            if self.ingest and self.ingest.latest:
                return 200, self.ingest.latest
            return 200, {}

        if path == "/api/events":
            limit = int(query.get("limit", ["200"])[0])
            if store is None:
                return 200, []
            return 200, store.recent_structural(limit=min(limit, 5000))

        if path == "/api/strategies":
            if oc is None:
                return 200, []
            return 200, [r.as_dict() for r in oc.registry.all()]

        if path.startswith("/api/strategies/"):
            rest = path[len("/api/strategies/"):]
            sid, _, action = rest.partition("/")
            if oc is None:
                return 404, {"error": "no controller"}
            rec = oc.registry.get(sid)
            if rec is None:
                return 404, {"error": f"unknown strategy {sid}"}

            if method == "GET" and not action:
                return 200, {**rec.as_dict(),
                             "live_history": oc.registry.live_history(sid)}
            if method == "POST" and action == "disable":
                if oc.live:
                    oc.live.disable(sid, "disabled via API")
                else:
                    oc.registry.set_status(sid, "retired", "disabled via API")
                return 200, {"ok": True, "strategy": sid, "status": "retired"}
            if method == "POST" and action == "deploy":
                live = oc._ensure_live_manager()
                ok = live.deploy(rec)
                return (200 if ok else 400), {"ok": ok, "strategy": sid}
            return 404, {"error": "unknown action"}

        if path == "/api/cycle" and method == "POST":
            if oc is None:
                return 404, {"error": "no controller"}
            trigger = "api"
            try:
                trigger = json.loads(body or b"{}").get("trigger", "api")
            except Exception:  # noqa: BLE001
                pass
            # Research is CPU-bound: run it off the event loop so the API and the
            # live manager keep responding while a cycle grinds.
            asyncio.create_task(asyncio.to_thread(oc.run_cycle, trigger))
            return 202, {"accepted": True, "trigger": trigger}

        return 404, {"error": f"no route for {method} {path}"}

    # ------------------------------------------------------------ websocket
    async def _websocket(self, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter, headers: dict,
                         query: dict) -> None:
        key = headers.get("sec-websocket-key")
        if not key:
            await self._respond(writer, 400, {"error": "missing Sec-WebSocket-Key"})
            return

        writer.write((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {_ws_accept(key)}\r\n\r\n"
        ).encode("latin-1"))
        await writer.drain()

        wanted = set(
            t.strip() for t in query.get("topics", ["structural,feature,live,control,log"])[0].split(",")
            if t.strip())
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._clients.add(q)
        peer = writer.get_extra_info("peername")
        log.info("ws client connected", extra={"peer": str(peer),
                                               "topics": ",".join(sorted(wanted))})

        async def sender():
            hello = json.dumps({"topic": "control", "data": {
                "msg": "connected", "topics": sorted(wanted),
                "state": self.openclaw.state if self.openclaw else "idle"}})
            writer.write(_ws_frame(hello.encode("utf-8")))
            await writer.drain()
            while True:
                msg = await q.get()
                try:
                    if json.loads(msg).get("topic") not in wanted:
                        continue
                except Exception:  # noqa: BLE001
                    pass
                writer.write(_ws_frame(msg.encode("utf-8")))
                await writer.drain()

        async def receiver():
            while True:
                frame = await _ws_read(reader)
                if frame is None:
                    return
                opcode, data = frame
                if opcode == 0x8:                      # close
                    return
                if opcode == 0x9:                      # ping -> pong
                    writer.write(_ws_frame(data, opcode=0xA))
                    await writer.drain()

        try:
            await asyncio.gather(sender(), receiver())
        except (asyncio.IncompleteReadError, ConnectionResetError,
                BrokenPipeError, asyncio.CancelledError, ValueError):
            pass
        finally:
            self._clients.discard(q)
            log.info("ws client disconnected", extra={"peer": str(peer)})
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass


# ----------------------------------------------------------------- runner
async def serve(cfg, bus: EventBus, openclaw=None, store=None, ingest=None,
                logger=None) -> ApiServer:
    api = ApiServer(cfg, bus, openclaw=openclaw, store=store, ingest=ingest,
                    logger=logger)
    await api.start()
    return api
