"""Module 2 - Bid & Ask Non-Print Structure Engines.

Two INDEPENDENT engines (one driven by the bid, one by the ask) with identical
logic and separate state, exactly as specified. Both are strictly incremental:
one tick in, O(1) work, bounded memory. Nothing is recomputed from history.

------------------------------------------------------------------------------
DEFINITIONS (these are the contract - the features and the DB depend on them)
------------------------------------------------------------------------------
Working on the side's quote ladder (bid engine watches `bid`, ask engine `ask`):

  Skipped price     A price level the quote JUMPED OVER: the quote moved from L1
                    to L2 where |L2 - L1| > 1 tick. Every level strictly between
                    them was skipped.

  Non-print event   A skipped level at which NO trade has printed inside the
                    lookback window. The book moved through a price and nobody
                    traded there - the core "non-print" signature.

  Liquidity void    The level opened by a non-print event. It stays OPEN until a
                    trade prints at that level (or it ages out / drifts far from
                    the market and is pruned).

  Structural gap    The whole jump, measured in ticks: gap = |L2 - L1| / ticksize.
                    A gap of N ticks produces N-1 skipped levels.

  Void persistence  How long a void survives. Reported two ways: the mean AGE of
                    currently-open voids, and an EMA of the lifetime of voids at
                    the moment they get filled.

  Structural        Levels traversed per second by the quote, EMA-smoothed.
  velocity          (Direction-signed: up is +, down is -.)

  Structural        d(velocity)/dt, EMA-smoothed. Positive = structure is
  acceleration      accelerating in its current direction.

Memory is bounded by pruning: printed-level history and open voids further than
`prune_distance_ticks` from the market (or older than `void_max_age_s`) are
dropped, because a void 300 ticks away is no longer market structure.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from .events import StructuralEvent
from .logging_setup import get_logger

log = get_logger("nonprint")


@dataclass
class NonPrintConfig:
    tick_size: float = 0.25
    # A level counts as "printed" if a trade hit it within this window.
    print_lookback_s: float = 300.0
    # Voids older than this are considered structurally dead and pruned.
    void_max_age_s: float = 1800.0
    # Voids further than this from the market are pruned (RAM guard).
    prune_distance_ticks: int = 400
    # EMA smoothing (0..1). Higher = faster response.
    velocity_alpha: float = 0.05
    accel_alpha: float = 0.05
    event_rate_alpha: float = 0.02
    # Safety cap: one absurd quote jump must not emit 10,000 events.
    max_skips_per_jump: int = 50

    @classmethod
    def from_config(cls, cfg) -> "NonPrintConfig":
        c = cfg.section("nonprint") if hasattr(cfg, "section") else dict(cfg)
        return cls(
            tick_size=float(cfg.get("instrument.tick_size", 0.25)
                            if hasattr(cfg, "get") else c.get("tick_size", 0.25)),
            print_lookback_s=float(c.get("print_lookback_s", 300.0)),
            void_max_age_s=float(c.get("void_max_age_s", 1800.0)),
            prune_distance_ticks=int(c.get("prune_distance_ticks", 400)),
            velocity_alpha=float(c.get("velocity_alpha", 0.05)),
            accel_alpha=float(c.get("accel_alpha", 0.05)),
            event_rate_alpha=float(c.get("event_rate_alpha", 0.02)),
            max_skips_per_jump=int(c.get("max_skips_per_jump", 50)),
        )


class NonPrintEngine:
    """One side's structure engine. Feed it ticks; it emits StructuralEvents."""

    def __init__(self, side: str, cfg: NonPrintConfig):
        assert side in ("bid", "ask")
        self.side = side
        self.cfg = cfg
        self.tick = cfg.tick_size

        # ---- incremental state (all O(1) or pruned)
        self.last_quote: float = 0.0
        self.last_ts: float = 0.0
        self.printed: dict[int, float] = {}     # level_key -> last print ts
        self.voids: dict[int, dict] = {}        # level_key -> {ts, gap_ticks}

        self.velocity: float = 0.0              # ticks/second, EMA, signed
        self.acceleration: float = 0.0          # ticks/second^2, EMA
        self.event_rate: float = 0.0            # non-print events/second, EMA

        # ---- cumulative counters
        self.non_print_events = 0
        self.skipped_prices = 0
        self.structural_gaps = 0
        self.voids_filled = 0

        # ---- rolling aggregates
        self.gap_size_ema: float = 0.0          # ticks
        self.fill_persistence_ema: float = 0.0  # seconds
        self.last_gap_ticks: int = 0
        self.quote_size: int = 0                # bid_size or ask_size (liquidity)
        self.recent_gaps: deque[int] = deque(maxlen=64)

        self.ticks_seen = 0

    # ---------------------------------------------------------------- helpers
    def _key(self, price: float) -> int:
        """Price -> integer level index on the tick grid (exact, hashable)."""
        return int(round(price / self.tick))

    def _price(self, key: int) -> float:
        return key * self.tick

    @staticmethod
    def _ema(prev: float, value: float, alpha: float) -> float:
        return value if prev == 0.0 else prev + alpha * (value - prev)

    # ------------------------------------------------------------------- feed
    def on_tick(self, tick) -> list[StructuralEvent]:
        """Process one tick. Returns the structural events it produced."""
        quote = tick.bid if self.side == "bid" else tick.ask
        if quote <= 0:                    # no quote in force yet
            return []

        self.ticks_seen += 1
        self.quote_size = tick.bid_size if self.side == "bid" else tick.ask_size
        events: list[StructuralEvent] = []
        ts = tick.ts

        # 1) A trade printed: record the level and fill any void sitting there.
        pk = self._key(tick.price)
        self.printed[pk] = ts
        void = self.voids.pop(pk, None)
        if void is not None:
            age = max(0.0, ts - void["ts"])
            self.voids_filled += 1
            self.fill_persistence_ema = self._ema(self.fill_persistence_ema, age, 0.1)
            events.append(StructuralEvent(
                ts=ts, side=self.side, kind="void_filled",
                price=self._price(pk), size=void["gap_ticks"] * self.tick,
                ticks=void["gap_ticks"], persistence=age))

        # 2) Did the quote move? That is where structure is created.
        if self.last_quote == 0.0:
            self.last_quote = quote
            self.last_ts = ts
            return events

        if quote != self.last_quote:
            prev_k = self._key(self.last_quote)
            new_k = self._key(quote)
            gap_ticks = abs(new_k - prev_k)
            dt = max(1e-6, ts - self.last_ts)

            if gap_ticks >= 2:
                self.structural_gaps += 1
                self.last_gap_ticks = gap_ticks
                self.recent_gaps.append(gap_ticks)
                self.gap_size_ema = self._ema(self.gap_size_ema, float(gap_ticks), 0.1)
                events.append(StructuralEvent(
                    ts=ts, side=self.side, kind="structural_gap",
                    price=quote, size=gap_ticks * self.tick, ticks=gap_ticks,
                    meta={"from": self.last_quote, "to": quote}))

                # Every level strictly between the old and new quote was skipped.
                step = 1 if new_k > prev_k else -1
                skipped = 0
                cutoff = ts - self.cfg.print_lookback_s
                for k in range(prev_k + step, new_k, step):
                    skipped += 1
                    if skipped > self.cfg.max_skips_per_jump:
                        break
                    self.skipped_prices += 1
                    events.append(StructuralEvent(
                        ts=ts, side=self.side, kind="skipped_price",
                        price=self._price(k), size=self.tick, ticks=1))

                    # No trade at that level inside the window -> NON-PRINT.
                    last_print = self.printed.get(k, -math.inf)
                    if last_print < cutoff:
                        self.non_print_events += 1
                        if k not in self.voids:
                            self.voids[k] = {"ts": ts, "gap_ticks": gap_ticks}
                            events.append(StructuralEvent(
                                ts=ts, side=self.side, kind="liquidity_void",
                                price=self._price(k), size=gap_ticks * self.tick,
                                ticks=gap_ticks))
                        events.append(StructuralEvent(
                            ts=ts, side=self.side, kind="non_print",
                            price=self._price(k), size=gap_ticks * self.tick,
                            ticks=gap_ticks))

            # 3) Velocity / acceleration on the quote ladder.
            signed_ticks = (new_k - prev_k)
            inst_velocity = signed_ticks / dt                 # ticks per second
            prev_velocity = self.velocity
            self.velocity = self._ema(self.velocity, inst_velocity,
                                      self.cfg.velocity_alpha)
            inst_accel = (self.velocity - prev_velocity) / dt
            self.acceleration = self._ema(self.acceleration, inst_accel,
                                          self.cfg.accel_alpha)

            n_events = sum(1 for e in events if e.kind == "non_print")
            self.event_rate = self._ema(self.event_rate, n_events / dt,
                                        self.cfg.event_rate_alpha)

            self.last_quote = quote
            self.last_ts = ts
            self._prune(ts, new_k)

        return events

    # ------------------------------------------------------------------ prune
    def _prune(self, ts: float, market_k: int) -> None:
        """Keep memory flat: drop far-away / stale structure."""
        if self.ticks_seen % 500:          # pruning every tick would be wasteful
            return
        dist = self.cfg.prune_distance_ticks
        max_age = self.cfg.void_max_age_s
        lo, hi = market_k - dist, market_k + dist

        dead = [k for k, v in self.voids.items()
                if k < lo or k > hi or (ts - v["ts"]) > max_age]
        for k in dead:
            self.voids.pop(k, None)

        cutoff = ts - self.cfg.print_lookback_s
        stale = [k for k, t in self.printed.items()
                 if t < cutoff or k < lo or k > hi]
        for k in stale:
            self.printed.pop(k, None)

    # --------------------------------------------------------------- snapshot
    def snapshot(self, ts: float) -> dict[str, float]:
        """Current structural state of this side, as flat named metrics."""
        s = self.side
        voids = self.voids
        if voids:
            ages = [ts - v["ts"] for v in voids.values()]
            sizes = [v["gap_ticks"] for v in voids.values()]
            mean_age = sum(ages) / len(ages)
            max_age = max(ages)
            mean_size = sum(sizes) / len(sizes)
        else:
            mean_age = max_age = mean_size = 0.0

        return {
            f"{s}_quote": self.last_quote,
            f"{s}_quote_size": float(self.quote_size),
            f"{s}_velocity": self.velocity,
            f"{s}_acceleration": self.acceleration,
            f"{s}_nonprint_count": float(self.non_print_events),
            f"{s}_nonprint_rate": self.event_rate,
            f"{s}_skipped_prices": float(self.skipped_prices),
            f"{s}_structural_gaps": float(self.structural_gaps),
            f"{s}_gap_size": self.gap_size_ema,
            f"{s}_last_gap_ticks": float(self.last_gap_ticks),
            f"{s}_void_open": float(len(voids)),
            f"{s}_void_size": mean_size,
            f"{s}_void_persistence": mean_age,
            f"{s}_void_max_persistence": max_age,
            f"{s}_void_fill_persistence": self.fill_persistence_ema,
            f"{s}_voids_filled": float(self.voids_filled),
        }

    def stats(self) -> dict:
        return {
            "side": self.side,
            "ticks": self.ticks_seen,
            "non_print_events": self.non_print_events,
            "skipped_prices": self.skipped_prices,
            "structural_gaps": self.structural_gaps,
            "open_voids": len(self.voids),
            "voids_filled": self.voids_filled,
        }


class DualNonPrintEngine:
    """The Bid and Ask engines side by side, kept independent by construction."""

    def __init__(self, cfg: NonPrintConfig):
        self.bid = NonPrintEngine("bid", cfg)
        self.ask = NonPrintEngine("ask", cfg)

    def on_tick(self, tick) -> list[StructuralEvent]:
        return self.bid.on_tick(tick) + self.ask.on_tick(tick)

    def snapshot(self, ts: float) -> dict[str, float]:
        out = self.bid.snapshot(ts)
        out.update(self.ask.snapshot(ts))
        return out

    def stats(self) -> dict:
        return {"bid": self.bid.stats(), "ask": self.ask.stats()}
