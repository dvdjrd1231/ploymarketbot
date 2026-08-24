"""Event model + async event bus - the spine of the event-driven architecture.

Everything upstream of the research bridge is a stream of immutable events:

    Tick  ->  StructuralEvent (non-print / void / gap)
          ->  LineBreakEvent  (one of the 100 resolution engines printed a line)
          ->  FeatureSnapshot (research row)
          ->  LiveEvent       (order / fill / degradation / deploy)

Producers publish; the event store, the live manager, the GUI and the WebSocket
API subscribe. Subscribers get their own bounded queue: a slow consumer (e.g. a
websocket on a laggy link) drops its OLDEST events rather than growing without
bound or back-pressuring the ingest loop. That is what keeps RAM flat under a
fast tick feed.

`slots=True` on the dataclasses matters here: at millions of ticks a __dict__
per event is the difference between a few hundred MB and a few GB.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

# ---------------------------------------------------------------- event types
TICK = "tick"
STRUCTURAL = "structural"
LINE_BREAK = "line_break"
FEATURE = "feature"
LIVE = "live"
CONTROL = "control"          # OpenClaw stage transitions


@dataclass(slots=True)
class Tick:
    """One row of IBKR Time & Sales, with the quote in force at that moment."""
    ts: float                # epoch seconds (float, sub-second precision)
    price: float             # last trade price
    size: int                # trade size
    bid: float = 0.0
    bid_size: int = 0
    ask: float = 0.0
    ask_size: int = 0

    def as_row(self) -> tuple:
        return (self.ts, self.price, self.size, self.bid, self.bid_size,
                self.ask, self.ask_size)


@dataclass(slots=True)
class StructuralEvent:
    """A non-print structure event emitted by the Bid or Ask engine."""
    ts: float
    side: str                # "bid" | "ask"
    kind: str                # non_print | liquidity_void | skipped_price |
                             # structural_gap | void_filled
    price: float             # level the event happened at
    size: float = 0.0        # gap size / void size in PRICE units
    ticks: int = 0           # size in instrument ticks
    persistence: float = 0.0  # seconds a void survived (void_filled only)
    meta: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> tuple:
        return (self.ts, self.side, self.kind, self.price, self.size,
                self.ticks, self.persistence)


@dataclass(slots=True)
class LineBreakEvent:
    """One line printed by one resolution engine."""
    ts: float
    resolution: int          # 1..100  (line break size in ticks)
    direction: int           # +1 up, -1 down
    open: float
    close: float
    reversal: bool           # True if this line flipped the trend
    run_length: int          # consecutive lines in this direction
    seconds: float           # time the line took to form
    ticks_consumed: int      # ticks processed while forming the line

    def as_row(self) -> tuple:
        return (self.ts, self.resolution, self.direction, self.open, self.close,
                int(self.reversal), self.run_length, self.seconds,
                self.ticks_consumed)


@dataclass(slots=True)
class FeatureSnapshot:
    """One research row: all engineered features at one instant."""
    ts: float
    price: float
    values: dict[str, float]


@dataclass(slots=True)
class LiveEvent:
    ts: float
    kind: str                # signal | order | fill | reject | deploy |
                             # disable | degradation | halt
    strategy_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def to_dict(evt: Any) -> dict:
    """JSON-safe projection of any event (used by the WS API and the log)."""
    if hasattr(evt, "__dataclass_fields__"):
        return asdict(evt)
    return dict(evt) if isinstance(evt, dict) else {"value": str(evt)}


# ------------------------------------------------------------------ event bus
class Subscription:
    """A bounded, drop-oldest queue of (topic, event) pairs."""

    __slots__ = ("topics", "queue", "dropped", "_bus")

    def __init__(self, bus: "EventBus", topics: Iterable[str], maxsize: int):
        self.topics = set(topics)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self._bus = bus

    def offer(self, topic: str, evt: Any) -> None:
        if self.topics and topic not in self.topics:
            return
        try:
            self.queue.put_nowait((topic, evt))
        except asyncio.QueueFull:
            # Drop the OLDEST so a slow consumer sees fresh data, not stale data,
            # and memory stays bounded no matter how far behind it falls.
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:  # pragma: no cover - race
                pass
            self.dropped += 1
            try:
                self.queue.put_nowait((topic, evt))
            except asyncio.QueueFull:  # pragma: no cover - race
                pass

    async def get(self) -> tuple[str, Any]:
        return await self.queue.get()

    def close(self) -> None:
        self._bus.unsubscribe(self)

    async def __aiter__(self):
        while True:
            yield await self.get()


class EventBus:
    """Minimal async pub/sub. No external dependency, no threads."""

    def __init__(self, queue_size: int = 2000):
        self._subs: list[Subscription] = []
        self._queue_size = queue_size
        self.published = 0

    def subscribe(self, *topics: str, maxsize: int | None = None) -> Subscription:
        sub = Subscription(self, topics, maxsize or self._queue_size)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def publish(self, topic: str, evt: Any) -> None:
        """Non-blocking fan-out. Safe to call from a hot ingest loop."""
        self.published += 1
        for sub in self._subs:
            sub.offer(topic, evt)

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    @property
    def dropped(self) -> int:
        return sum(s.dropped for s in self._subs)
