"""
Broadcast hub between the engine and every connected browser tab.

Design rules that keep the UI smooth:

  * **The engine never awaits a browser.** :meth:`Hub.publish` is a plain
    (non-async) call that drops a frame into each client's pending set and
    returns. A slow or wedged tab can never back-pressure the trading loop.
  * **Newest-wins coalescing.** If a client falls behind, superseded *snapshot*
    frames (positions, account, stats, orders …) are replaced rather than piled
    up, so a tab that was backgrounded catches up on the current truth instead
    of replaying a stale backlog. Append-only frames (log lines, toasts) are
    kept in order and dropped oldest-first once the buffer is full.
  * **Last-value cache.** A newly connected tab gets a full snapshot
    immediately, so there is no blank-dashboard flash on refresh or reconnect.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Optional

# Frames where only the most recent value matters. Sending an older one after a
# newer one would show stale data, so the buffer keeps just the latest.
COALESCING = frozenset({
    "state", "account", "positions", "stats", "orders", "history",
    "settings", "feeds", "target", "equity",
})

# Order in which a fresh connection is primed.
SNAPSHOT_ORDER = ("state", "settings", "account", "positions", "orders",
                  "history", "stats", "feeds", "target", "equity")

MAX_BUFFERED = 200


class Client:
    """One connected browser tab."""

    __slots__ = ("id", "_pending", "_order", "_wake", "dropped", "connected_ts")

    def __init__(self, client_id: int):
        self.id = client_id
        self._pending: dict[str, dict] = {}
        self._order: deque[str] = deque()
        self._wake = asyncio.Event()
        self.dropped = 0
        self.connected_ts = time.time()

    def put(self, message: dict) -> None:
        """Buffer a frame, coalescing snapshots and trimming overflow."""
        kind = message["type"]
        if kind in COALESCING:
            slot = kind
            if slot not in self._pending:
                self._order.append(slot)
        else:
            # Append-only frames get a unique slot so none are merged away.
            slot = f"{kind}\x00{time.perf_counter_ns()}"
            self._order.append(slot)
        self._pending[slot] = message

        while len(self._order) > MAX_BUFFERED:
            oldest = self._order.popleft()
            if self._pending.pop(oldest, None) is not None:
                self.dropped += 1

        self._wake.set()

    def drain(self) -> list[dict]:
        """Take everything buffered, in the order it was published."""
        out: list[dict] = []
        while self._order:
            slot = self._order.popleft()
            message = self._pending.pop(slot, None)
            if message is not None:
                out.append(message)
        return out

    async def wait(self) -> None:
        """Park until at least one frame is buffered."""
        await self._wake.wait()
        self._wake.clear()


class Hub:
    def __init__(self) -> None:
        self._clients: dict[int, Client] = {}
        self._next_id = 1
        # Last value of every coalescing frame, replayed to new connections.
        self._snapshot: dict[str, dict] = {}

    # -- client lifecycle ----------------------------------------------------

    def connect(self) -> Client:
        client = Client(self._next_id)
        self._next_id += 1
        self._clients[client.id] = client
        for kind in SNAPSHOT_ORDER:
            message = self._snapshot.get(kind)
            if message is not None:
                client.put(message)
        return client

    def disconnect(self, client: Client) -> None:
        self._clients.pop(client.id, None)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # -- publishing ----------------------------------------------------------

    def publish(self, kind: str, data: Any) -> None:
        """Fan a frame out to every client. Never blocks, never raises."""
        message = {"type": kind, "data": data, "ts": time.time()}
        if kind in COALESCING:
            self._snapshot[kind] = message
        for client in list(self._clients.values()):
            try:
                client.put(message)
            except Exception:
                pass

    def cached(self, kind: str) -> Optional[Any]:
        message = self._snapshot.get(kind)
        return message["data"] if message else None

    def reset_cache(self, *kinds: str) -> None:
        for kind in kinds:
            self._snapshot.pop(kind, None)
