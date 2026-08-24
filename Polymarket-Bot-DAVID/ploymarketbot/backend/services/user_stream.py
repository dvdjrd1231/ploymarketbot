"""
Authenticated CLOB *user* channel.

Subscribing to ``wss://ws-subscriptions-clob.polymarket.com/ws/user`` with the
L2 API credentials gives push notifications for our own activity:

  * ``order``  — placement / update / cancellation, with the running
    ``size_matched``. This is what makes order status live rather than polled.
  * ``trade``  — a fill working its way through MATCHED → MINED → CONFIRMED.

Without this channel the only way to know an order's fate is to poll
``GET /orders``, which is exactly the kind of lag the rebuild is meant to
eliminate. The engine still reconciles over REST on a slow timer as a backstop,
so a dropped socket degrades gracefully instead of silently desynchronising.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Awaitable, Callable, Iterable, Optional

import websockets

Handler = Callable[[str, dict], Awaitable[None]]


class UserStream:
    def __init__(self, ws_host: str, auth: dict, on_event: Handler):
        self.ws_url = f"{ws_host.rstrip('/')}/user"
        self._auth = auth
        self._on_event = on_event

        self._markets: set[str] = set()
        self._resubscribe = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._running = False

        self.state = "idle"     # idle | connecting | live | retrying
        self.error = ""
        self.last_message_ts = 0.0
        self.reconnects = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="user-stream")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self.state = "idle"

    def track_markets(self, market_ids: Iterable[str]) -> None:
        """Watch these condition ids. Reconnects if the set actually changed."""
        new = {str(m) for m in market_ids if m}
        if new == self._markets:
            return
        self._markets = new
        self._resubscribe.set()

    def health(self) -> dict:
        return {
            "state": self.state,
            "error": self.error,
            "markets": len(self._markets),
            "lastMessageTs": self.last_message_ts,
            "reconnects": self.reconnects,
        }

    # -- internals -----------------------------------------------------------

    async def _loop(self) -> None:
        backoff = 1.0
        while self._running:
            if not self._markets:
                self.state = "idle"
                self._resubscribe.clear()
                try:
                    await asyncio.wait_for(self._resubscribe.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                continue

            markets = sorted(self._markets)
            self.state = "connecting"
            resubscribing = False
            try:
                async with websockets.connect(
                    self.ws_url, ping_interval=15, ping_timeout=20,
                    close_timeout=5, open_timeout=15,
                ) as ws:
                    await ws.send(json.dumps({
                        "auth": self._auth,
                        "markets": markets,
                        "type": "user",
                    }))
                    self.state = "live"
                    self.error = ""
                    backoff = 1.0
                    self._resubscribe.clear()
                    resubscribing = await self._pump(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"

            if not self._running:
                break
            if resubscribing:
                continue
            self.state = "retrying"
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _pump(self, ws) -> bool:
        keepalive = asyncio.create_task(self._keepalive(ws))
        watcher = asyncio.create_task(self._resubscribe.wait())
        receiver: Optional[asyncio.Task] = None
        try:
            while self._running:
                receiver = asyncio.create_task(ws.recv())
                done, _ = await asyncio.wait(
                    {receiver, watcher}, return_when=asyncio.FIRST_COMPLETED
                )
                if watcher in done:
                    return True
                raw = receiver.result()
                receiver = None
                await self._dispatch(raw)
            return False
        finally:
            # `receiver` may still be in flight if this loop was cancelled;
            # awaiting it retrieves the socket-teardown exception.
            for task in (keepalive, watcher, receiver):
                if task is None:
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _keepalive(self, ws) -> None:
        while True:
            await asyncio.sleep(10)
            with contextlib.suppress(Exception):
                await ws.send("PING")

    async def _dispatch(self, raw) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        text = (raw or "").strip()
        if not text or text.upper() in ("PONG", "PING"):
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        self.last_message_ts = time.time()
        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            if not isinstance(event, dict):
                continue
            kind = event.get("event_type") or event.get("eventType") or ""
            if kind in ("order", "trade"):
                with contextlib.suppress(Exception):
                    await self._on_event(kind, event)
