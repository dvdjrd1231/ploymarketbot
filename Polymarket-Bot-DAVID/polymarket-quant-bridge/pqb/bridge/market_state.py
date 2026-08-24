"""
The Market-State layer: EVENTS -> STATE -> LEAN.

The operator's architecture, implemented exactly as briefed: a lightweight
layer that consumes the normalized event stream (book observations + trade
prints, both already flowing through the bot) and maintains, per market, the
answer to "what is happening in this market RIGHT NOW?" — rolling changes over
several windows, three 0-100 scores, and one state classification:

    DORMANT -> DEVELOPING -> IMPULSE -> CONTINUATION -> EXHAUSTION -> REVERSAL

Deliberately simple on purpose (his words: do not over-engineer): rolling
windows, plain arithmetic, and thresholds that live in configuration so they
can be optimised from recorded data later. No single indicator is the master
signal — every score is a blend, and the layer makes NO trading decisions.
LEAN consumes the snapshot; deciding remains LEAN's job.

Persistence comes free by construction: the snapshot's ``ms_*`` columns ride
on every captured research row (the replayable record his step 9 asks for),
and every LEAN decision already journals the features it saw.
"""

from __future__ import annotations

import collections
import math
import time
from dataclasses import dataclass
from typing import Optional

# The rolling windows, in seconds. The sub-5s windows exist in the contract
# but honesty requires saying: at a 2-second cycle they are sparse, and their
# values read 0 when no events landed inside them. They become meaningful as
# cadence rises; nothing downstream assumes they are dense.
WINDOWS = (1, 5, 15, 30, 60, 300)
LONG_WINDOW = 300
SHORT_WINDOW = 15

# States, exported as both names and stable numeric codes (columns are floats).
STATES = ("dormant", "developing", "impulse", "continuation",
          "exhaustion", "reversal")
STATE_CODE = {name: float(i) for i, name in enumerate(STATES)}


@dataclass
class _Obs:
    """One normalized observation of a market: a book look or a print."""

    ts: float
    price: float
    notional: float = 0.0          # traded USDC (0 for book-only looks)
    is_trade: bool = False
    buy: bool = False
    depth: float = 0.0             # bid_depth + ask_depth at this look


class _EMA:
    """Mean and deviation that age gracefully — the market's own baseline."""

    def __init__(self, alpha: float = 0.05):
        self.alpha = alpha
        self.mean: Optional[float] = None
        self.dev: float = 0.0

    def update(self, value: float) -> None:
        if self.mean is None:
            self.mean = value
            return
        delta = value - self.mean
        self.mean += self.alpha * delta
        self.dev = (1 - self.alpha) * (self.dev + self.alpha * abs(delta))

    def z(self, value: float) -> float:
        if self.mean is None or self.dev < 1e-9:
            return 0.0
        return (value - self.mean) / self.dev


class _TokenState:
    """Everything the layer knows about one market, updated per event."""

    def __init__(self):
        self.obs: collections.deque[_Obs] = collections.deque()
        self.last_price = 0.0
        self.imbalance = 0.0           # buy-vs-sell notional share, -1..1
        # Impulse sequences: consecutive related events grouped under one id.
        # A new sequence begins when a market ENTERS the impulse family from
        # quiet; the id persists through continuation/exhaustion/reversal so
        # everything belonging to one move can be studied as one move.
        self.sequence_id = 0
        self.prev_state = "dormant"
        # The market's OWN baselines — anomaly is "unusual for this market",
        # never one universal dollar threshold for every market.
        self.base_rate = _EMA()        # trades per minute
        self.base_size = _EMA()        # per-print notional
        self.base_move = _EMA()        # |price move| per window

    def add(self, obs: _Obs) -> None:
        self.obs.append(obs)
        if obs.price > 0:
            self.last_price = obs.price
        cutoff = obs.ts - LONG_WINDOW - 60
        while self.obs and self.obs[0].ts < cutoff:
            self.obs.popleft()

    def window(self, now: float, seconds: float) -> list[_Obs]:
        cutoff = now - seconds
        return [o for o in self.obs if o.ts >= cutoff]

    def price_change(self, now: float, seconds: float) -> float:
        """Fractional price change across the window; 0 when undefined."""
        rows = [o for o in self.window(now, seconds) if o.price > 0]
        if len(rows) < 2 or rows[0].price <= 0:
            return 0.0
        return (rows[-1].price - rows[0].price) / rows[0].price

    def volume_between(self, start: float, end: float) -> float:
        """Traded notional in [start, end)."""
        return sum(o.notional for o in self.obs
                   if o.is_trade and start <= o.ts < end)

    def volume_change(self, now: float, seconds: float) -> float:
        """Volume in the window minus volume in the equal window before it —
        positive when the tape is accelerating, negative when it is drying."""
        recent = self.volume_between(now - seconds, now)
        prior = self.volume_between(now - 2 * seconds, now - seconds)
        return recent - prior


class MarketStateTracker:
    """The state layer itself: feed events in, read one clean snapshot out."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._tokens: dict[str, _TokenState] = {}
        self._trade_seen: dict[str, float] = {}     # token -> last trade ts fed

    def _state_for(self, token_id: str) -> _TokenState:
        state = self._tokens.get(token_id)
        if state is None:
            state = _TokenState()
            self._tokens[token_id] = state
        return state

    # -- event intake --------------------------------------------------------

    def feed_book(self, token_id: str, ts: float, price: float,
                  bid_depth: float, ask_depth: float) -> None:
        if price <= 0:
            return
        self._state_for(token_id).add(_Obs(
            ts=ts, price=price, depth=bid_depth + ask_depth))

    def feed_trade(self, token_id: str, ts: float, price: float,
                   notional: float, is_buy: bool) -> bool:
        """Returns True when the event was new — the caller persists exactly
        the raw events this layer actually consumed, nothing else."""
        if price <= 0:
            return False
        last = self._trade_seen.get(token_id, 0.0)
        if ts <= last:
            return False               # tape overlap between cycles
        self._trade_seen[token_id] = ts
        state = self._state_for(token_id)
        state.add(_Obs(ts=ts, price=price, notional=max(0.0, notional),
                       is_trade=True, buy=is_buy))
        state.base_size.update(max(0.0, notional))
        return True

    # -- the snapshot --------------------------------------------------------

    def snapshot(self, token_id: str, now: Optional[float] = None
                 ) -> dict[str, float]:
        state = self._tokens.get(token_id)
        if state is None or not state.obs:
            return {}
        now = time.time() if now is None else now

        out: dict[str, float] = {}
        for seconds in WINDOWS:
            out[f"ms_chg_{seconds}s"] = state.price_change(now, seconds)
        # Per-window volume change (the MarketState object's VolumeChange1s..1m):
        # this window's notional against the equal window preceding it.
        for seconds in (1, 5, 15, 60):
            out[f"ms_vol_chg_{seconds}s"] = state.volume_change(now, seconds)

        long_rows = state.window(now, LONG_WINDOW)
        trades = [o for o in long_rows if o.is_trade]
        short_trades = [o for o in trades if o.ts >= now - SHORT_WINDOW]
        span_min = LONG_WINDOW / 60.0
        rate = len(trades) / span_min                       # prints per minute
        short_rate = len(short_trades) * 60.0 / SHORT_WINDOW
        volume = sum(o.notional for o in trades)
        buys = sum(o.notional for o in trades if o.buy)
        sells = volume - buys
        imbalance = ((buys - sells) / volume) if volume > 0 else 0.0
        state.imbalance = imbalance

        depths = [o.depth for o in long_rows if o.depth > 0]
        depth_now = depths[-1] if depths else 0.0
        depth_then = depths[0] if depths else 0.0
        liquidity_change = ((depth_now - depth_then) / depth_then
                            if depth_then > 0 else 0.0)

        out.update({
            "ms_trade_rate": rate,
            "ms_trade_rate_short": short_rate,
            "ms_volume": volume,
            "ms_imbalance": imbalance,
            "ms_liquidity_chg": liquidity_change,
            # Data quality: how much of the long window actually has events.
            # A score computed from two observations must be readable as such.
            "ms_data_quality": min(1.0, len(long_rows) / 30.0),
        })

        move_long = out[f"ms_chg_{LONG_WINDOW}s"]
        move_short = out[f"ms_chg_{SHORT_WINDOW}s"]

        impulse = self._impulse_score(state, now, move_long, move_short,
                                      rate, short_rate, imbalance,
                                      liquidity_change)
        exhaustion = self._exhaustion_score(move_long, move_short, rate,
                                            short_rate, volume, trades, now,
                                            liquidity_change)
        anomaly = self._anomaly_score(state, trades, rate, move_long)

        out["ms_impulse"] = impulse
        out["ms_exhaustion"] = exhaustion
        out["ms_anomaly"] = anomaly
        name = self._classify(state, impulse, exhaustion, move_long,
                              move_short, rate, trades)
        out["ms_state"] = STATE_CODE[name]
        # Sequence id: a new one the moment a market enters the impulse family
        # from quiet; the same id rides through continuation, exhaustion and
        # reversal, and 0 means "no sequence in progress".
        active = ("impulse", "continuation", "exhaustion", "reversal")
        if name in active and state.prev_state not in active:
            state.sequence_id += 1
        state.prev_state = name
        out["ms_sequence_id"] = float(state.sequence_id if name in active
                                      else 0)

        # Baselines learn AFTER scoring, so today's abnormality is measured
        # against yesterday's normal rather than absorbed into it instantly.
        state.base_rate.update(rate)
        state.base_move.update(abs(move_long))
        return out

    # -- scores (0-100, every one a blend — no master signal) ----------------

    @staticmethod
    def _scale(value: float, full_at: float) -> float:
        """0..1, saturating at ``full_at``."""
        if full_at <= 0:
            return 0.0
        return max(0.0, min(1.0, value / full_at))

    def _impulse_score(self, state, now, move_long, move_short, rate,
                       short_rate, imbalance, liquidity_change) -> float:
        move = self._scale(abs(move_long), 0.08)            # 8% in 5m saturates
        velocity = self._scale(abs(move_short), 0.03)       # 3% in 15s
        accel = self._scale(short_rate / rate if rate > 0 else 0.0, 3.0)
        direction = abs(imbalance)
        agree = 1.0 if (move_long * imbalance) > 0 else 0.3
        support = 1.0 if liquidity_change >= -0.2 else 0.5  # book not fleeing
        raw = (0.30 * move + 0.25 * velocity + 0.20 * accel
               + 0.25 * direction) * agree * support
        return round(100.0 * min(1.0, raw), 2)

    def _exhaustion_score(self, move_long, move_short, rate, short_rate,
                          volume, trades, now, liquidity_change) -> float:
        if abs(move_long) < self.cfg.min_move_pct:
            return 0.0                  # nothing happened; nothing to exhaust
        # The move exists but its engine is dying: short-window velocity,
        # participation and volume all fading against the window that made it.
        velocity_fade = 1.0 - self._scale(
            abs(move_short), max(1e-9, abs(move_long) * 0.3))
        rate_fade = 1.0 - self._scale(short_rate / rate if rate > 0 else 0.0,
                                      1.0)
        recent_vol = sum(o.notional for o in trades
                         if o.ts >= now - SHORT_WINDOW)
        vol_share = recent_vol / volume if volume > 0 else 0.0
        vol_fade = 1.0 - self._scale(vol_share, SHORT_WINDOW / LONG_WINDOW * 2)
        drain = self._scale(-liquidity_change, 0.4)
        raw = 0.35 * velocity_fade + 0.30 * rate_fade + 0.25 * vol_fade \
            + 0.10 * drain
        return round(100.0 * min(1.0, raw), 2)

    def _anomaly_score(self, state, trades, rate, move_long) -> float:
        """Unusual for THIS market, against its own learned baseline."""
        zs = [state.base_rate.z(rate), state.base_move.z(abs(move_long))]
        if trades:
            largest = max(o.notional for o in trades)
            zs.append(state.base_size.z(largest))
        peak = max(zs) if zs else 0.0
        return round(100.0 * self._scale(peak, 4.0), 2)    # 4 sigma saturates

    # -- classification ------------------------------------------------------

    def _classify(self, state, impulse, exhaustion, move_long, move_short,
                  rate, trades) -> str:
        cfg = self.cfg
        if len(trades) <= cfg.dormant_max_trades:
            return "dormant"
        # Reversal first: the short move running hard against the long move
        # is its own condition, whatever the scores say.
        if (abs(move_long) >= cfg.min_move_pct
                and move_long * move_short < 0
                and abs(move_short) >= cfg.reversal_fraction * abs(move_long)):
            return "reversal"
        if exhaustion >= cfg.exhaustion_score_min \
                and abs(move_long) >= cfg.min_move_pct:
            return "exhaustion"
        if impulse >= cfg.impulse_score_min:
            return "impulse"
        if (impulse >= cfg.continuation_score_min
                and abs(move_long) >= cfg.min_move_pct
                and move_long * move_short >= 0):
            return "continuation"
        baseline = state.base_rate.mean
        if baseline is not None and rate >= cfg.developing_activity_x * baseline \
                and rate > 0:
            return "developing"
        if baseline is None and rate > 0:
            return "developing"        # first life ever seen in this market
        return "dormant"

    def prune(self, now: float, idle_seconds: float = 7_200.0) -> int:
        """Forget tokens with no events for hours — long-run memory hygiene."""
        stale = [token for token, state in self._tokens.items()
                 if not state.obs or now - state.obs[-1].ts > idle_seconds]
        for token in stale:
            self._tokens.pop(token, None)
            self._trade_seen.pop(token, None)
        return len(stale)

    def stats(self) -> dict:
        return {"tokens": len(self._tokens)}
