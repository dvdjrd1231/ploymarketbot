"""Module 3 - Multi-Resolution Line Break Structure Engine (100 engines).

Generates 100 INDEPENDENT line-break engines at resolutions

        1 Line Break / 1 Tick   ...   100 Line Break / 1 Tick

i.e. engine R prints a new line only when price travels R ticks. Each engine
keeps its own structural state (direction, current line, run length, reversal
count); they are never mixed. Research then asks: *which structural resolution
currently carries the edge?* - which is the entire point of running all 100.

------------------------------------------------------------------------------
LINE BREAK RULE (event-driven - a line prints only when price earns it)
------------------------------------------------------------------------------
With threshold T = R * tick_size and `reversal_lines` = k (default 1):

  continuation   dir=+1 and price >= close + T   -> new UP line   (open=close)
                 dir=-1 and price <= close - T   -> new DOWN line (open=close)

  reversal       dir=+1 and price <= (lowest open of last k lines) - T
                     -> DOWN line, run resets, reversal counted
                 dir=-1 and price >= (highest open of last k lines) + T
                     -> UP line, run resets, reversal counted

  seed           no direction yet: the first move of T ticks from the seed price
                 sets the initial direction.

k=1 is the classic single line break; k=3 reproduces the traditional 3-line
break (a reversal must clear the last three lines). Line break charts FILTER
noise by construction - no time axis, only structure - which is precisely why
the client uses them, so we never resample them onto a clock.

------------------------------------------------------------------------------
WHY NUMPY ARRAYS INSTEAD OF 100 OBJECTS
------------------------------------------------------------------------------
Feeding every tick to 100 Python objects is ~100 attribute lookups + compares
per tick; at millions of ticks that dominates the whole run. Instead all engine
state lives in parallel numpy arrays, and each engine caches its next UP and
DOWN trigger price. A tick that moves nothing (the overwhelming majority) costs
ONE vectorized comparison over a 100-element array; only the engines that
actually triggered are touched in Python. That is what holds CPU down while
still running all 100 resolutions on raw ticks.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .events import LineBreakEvent
from .logging_setup import get_logger

log = get_logger("linebreak")

_BIG = 1e18      # "unreachable" trigger sentinel


@dataclass
class LineBreakConfig:
    tick_size: float = 0.25
    min_resolution: int = 1
    max_resolution: int = 100
    reversal_lines: int = 1        # 1 = classic line break, 3 = 3-line break
    # Which engine's line prints drive a research row (event-driven sampling).
    snapshot_resolution: int = 1
    # Persist line events only for these resolutions (RAM/disk guard); every
    # resolution is still COMPUTED and feeds the consensus features.
    store_stride: int = 5

    @classmethod
    def from_config(cls, cfg) -> "LineBreakConfig":
        c = cfg.section("structure") if hasattr(cfg, "section") else dict(cfg)
        return cls(
            tick_size=float(cfg.get("instrument.tick_size", 0.25)),
            min_resolution=int(c.get("min_resolution", 1)),
            max_resolution=int(c.get("max_resolution", 100)),
            reversal_lines=int(c.get("reversal_lines", 1)),
            snapshot_resolution=int(c.get("snapshot_resolution", 1)),
            store_stride=int(c.get("store_stride", 5)),
        )


class MultiResolutionLineBreak:
    """All 100 line-break engines, driven tick by tick."""

    def __init__(self, cfg: LineBreakConfig):
        self.cfg = cfg
        self.tick = cfg.tick_size
        self.resolutions = np.arange(cfg.min_resolution, cfg.max_resolution + 1,
                                     dtype=np.int32)
        k = len(self.resolutions)
        self.k = k
        self.thr = self.resolutions.astype(np.float64) * self.tick

        # ---- per-engine state (parallel arrays)
        self.dir = np.zeros(k, dtype=np.int8)          # -1 / 0 / +1
        self.l_open = np.zeros(k, dtype=np.float64)
        self.l_close = np.zeros(k, dtype=np.float64)
        self.run = np.zeros(k, dtype=np.int32)         # consecutive same-dir lines
        self.lines = np.zeros(k, dtype=np.int64)       # total lines printed
        self.reversals = np.zeros(k, dtype=np.int64)
        self.line_ts = np.zeros(k, dtype=np.float64)   # ts the current line opened
        self.line_ticks = np.zeros(k, dtype=np.int64)  # ticks consumed by it

        # EMAs used by the compression / expansion features
        self.ticks_per_line = np.zeros(k, dtype=np.float64)
        self.secs_per_line = np.zeros(k, dtype=np.float64)
        self.reversal_rate = np.zeros(k, dtype=np.float64)

        # Rolling opens of the last `reversal_lines` lines -> reversal reference.
        self.rev_k = max(1, cfg.reversal_lines)
        self.recent_opens = np.zeros((k, self.rev_k), dtype=np.float64)
        self.n_opens = np.zeros(k, dtype=np.int32)

        # Cached trigger prices (the fast path).
        self.up_trig = np.full(k, _BIG, dtype=np.float64)
        self.dn_trig = np.full(k, -_BIG, dtype=np.float64)

        self.seeded = False
        self.ticks_seen = 0
        self.snap_idx = int(np.clip(cfg.snapshot_resolution - cfg.min_resolution,
                                    0, k - 1))
        self.store_mask = np.zeros(k, dtype=bool)
        stride = max(1, cfg.store_stride)
        self.store_mask[::stride] = True
        self.store_mask[self.snap_idx] = True

    # ------------------------------------------------------------------ seed
    def _seed(self, price: float, ts: float) -> None:
        self.l_open[:] = price
        self.l_close[:] = price
        self.line_ts[:] = ts
        self.up_trig[:] = price + self.thr
        self.dn_trig[:] = price - self.thr
        self.seeded = True

    # ------------------------------------------------------------- triggers
    def _refresh_trigger(self, i: int) -> None:
        """Recompute engine i's next up/down trigger from its state."""
        t = self.thr[i]
        d = self.dir[i]
        if d > 0:
            self.up_trig[i] = self.l_close[i] + t
            self.dn_trig[i] = self._rev_low(i) - t
        elif d < 0:
            self.dn_trig[i] = self.l_close[i] - t
            self.up_trig[i] = self._rev_high(i) + t
        else:
            self.up_trig[i] = self.l_close[i] + t
            self.dn_trig[i] = self.l_close[i] - t

    def _rev_low(self, i: int) -> float:
        n = int(self.n_opens[i])
        if n == 0:
            return self.l_open[i]
        return float(self.recent_opens[i, :min(n, self.rev_k)].min())

    def _rev_high(self, i: int) -> float:
        n = int(self.n_opens[i])
        if n == 0:
            return self.l_open[i]
        return float(self.recent_opens[i, :min(n, self.rev_k)].max())

    def _push_open(self, i: int, value: float) -> None:
        if self.rev_k == 1:
            self.recent_opens[i, 0] = value
        else:
            self.recent_opens[i, 1:] = self.recent_opens[i, :-1]
            self.recent_opens[i, 0] = value
        self.n_opens[i] = min(self.n_opens[i] + 1, self.rev_k)

    # ------------------------------------------------------------------ feed
    def on_tick(self, tick) -> list[LineBreakEvent]:
        price = tick.price
        ts = tick.ts
        self.ticks_seen += 1

        if not self.seeded:
            self._seed(price, ts)
            return []

        self.line_ticks += 1          # every engine's current line consumed a tick

        # FAST PATH: one vectorized test over all 100 engines.
        hit = (price >= self.up_trig) | (price <= self.dn_trig)
        if not hit.any():
            return []

        events: list[LineBreakEvent] = []
        for i in np.flatnonzero(hit):
            events.extend(self._advance(int(i), price, ts))
        return events

    def _advance(self, i: int, price: float, ts: float) -> list[LineBreakEvent]:
        """Print however many lines engine i has earned at this price."""
        out: list[LineBreakEvent] = []
        t = float(self.thr[i])
        guard = 0

        while guard < 64:              # a huge gap can legitimately print many lines
            guard += 1
            d = int(self.dir[i])
            up_hit = price >= self.up_trig[i]
            dn_hit = price <= self.dn_trig[i]
            if not (up_hit or dn_hit):
                break

            if d == 0:
                new_dir = 1 if up_hit else -1
                reversal = False
            elif d > 0:
                if up_hit:
                    new_dir, reversal = 1, False
                else:
                    new_dir, reversal = -1, True
            else:
                if dn_hit:
                    new_dir, reversal = -1, False
                else:
                    new_dir, reversal = 1, True

            # The line closes exactly on the level it earned, snapped to the
            # tick grid - line break levels are structure, not raw prints.
            if new_dir > 0:
                base = float(self.l_close[i]) if not reversal else self._rev_high(i)
                close = base + t
                close = min(close, self._snap(price))
                close = max(close, base + t)
            else:
                base = float(self.l_close[i]) if not reversal else self._rev_low(i)
                close = base - t
                close = max(close, self._snap(price))
                close = min(close, base - t)

            open_ = base
            secs = max(0.0, ts - float(self.line_ts[i]))
            nticks = int(self.line_ticks[i])

            if reversal or d == 0 or new_dir != d:
                self.run[i] = 1
                if reversal:
                    self.reversals[i] += 1
            else:
                self.run[i] += 1

            self.dir[i] = new_dir
            self.l_open[i] = open_
            self.l_close[i] = close
            self.lines[i] += 1
            self.line_ts[i] = ts
            self.line_ticks[i] = 0
            self._push_open(i, open_)

            # EMAs behind the compression / expansion features.
            a = 0.1
            self.ticks_per_line[i] = (nticks if self.ticks_per_line[i] == 0
                                      else self.ticks_per_line[i] + a * (nticks - self.ticks_per_line[i]))
            self.secs_per_line[i] = (secs if self.secs_per_line[i] == 0
                                     else self.secs_per_line[i] + a * (secs - self.secs_per_line[i]))
            rv = 1.0 if reversal else 0.0
            self.reversal_rate[i] = self.reversal_rate[i] + a * (rv - self.reversal_rate[i])

            self._refresh_trigger(i)

            out.append(LineBreakEvent(
                ts=ts, resolution=int(self.resolutions[i]), direction=new_dir,
                open=open_, close=close, reversal=reversal,
                run_length=int(self.run[i]), seconds=secs, ticks_consumed=nticks))
        return out

    def _snap(self, price: float) -> float:
        return round(price / self.tick) * self.tick

    # --------------------------------------------------------------- readout
    def is_snapshot_line(self, events: list[LineBreakEvent]) -> bool:
        """True when the reference resolution printed - i.e. emit a research row."""
        ref = int(self.resolutions[self.snap_idx])
        return any(e.resolution == ref for e in events)

    def should_store(self, resolution: int) -> bool:
        i = int(resolution - self.cfg.min_resolution)
        return bool(0 <= i < self.k and self.store_mask[i])

    def snapshot(self) -> dict[str, float]:
        """Cross-resolution structural state -> the consensus feature block."""
        d = self.dir.astype(np.float64)
        active = self.lines > 0
        n_active = int(active.sum())
        if n_active == 0:
            return {
                "res_consensus": 0.0, "res_agreement": 0.0, "res_divergence": 1.0,
                "res_up_fraction": 0.0, "res_trend_persistence": 0.0,
                "res_structural_volatility": 0.0, "res_reversal_frequency": 0.0,
                "res_compression": 1.0, "res_expansion": 1.0,
                "res_lines_total": 0.0, "res_active": 0.0,
            }

        da = d[active]
        consensus = float(da.mean())                       # -1..+1
        up_frac = float((da > 0).mean())
        # Agreement = how lopsided the vote is (1 = unanimous, 0 = 50/50).
        agreement = abs(2.0 * up_frac - 1.0)
        divergence = 1.0 - agreement
        volatility = float(da.std())

        runs = self.run[active].astype(np.float64)
        persistence = float(runs.mean())
        rev_freq = float(self.reversal_rate[active].mean())

        # Compression / expansion: how long the CURRENT lines are taking to form
        # versus their own long-run pace. Slow lines (>1) = compression (price
        # grinding, structure stalling); fast lines (<1) = expansion (impulse).
        tpl = self.ticks_per_line[active]
        cur = self.line_ticks[active].astype(np.float64)
        base = np.where(tpl > 0, tpl, 1.0)
        ratio = float(np.clip(cur / base, 0.0, 10.0).mean())
        compression = ratio
        expansion = 1.0 / ratio if ratio > 1e-9 else 10.0

        return {
            "res_consensus": consensus,
            "res_agreement": agreement,
            "res_divergence": divergence,
            "res_up_fraction": up_frac,
            "res_trend_persistence": persistence,
            "res_structural_volatility": volatility,
            "res_reversal_frequency": rev_freq,
            "res_compression": compression,
            "res_expansion": min(expansion, 10.0),
            "res_lines_total": float(self.lines.sum()),
            "res_active": float(n_active),
        }

    def per_resolution(self, stride: int | None = None) -> dict[str, float]:
        """Per-engine columns for the exported dataset (lb_res{R}_*)."""
        stride = max(1, stride or self.cfg.store_stride)
        out: dict[str, float] = {}
        for i in range(0, self.k, stride):
            r = int(self.resolutions[i])
            out[f"lb_res{r}_dir"] = float(self.dir[i])
            out[f"lb_res{r}_run"] = float(self.run[i])
            out[f"lb_res{r}_close"] = float(self.l_close[i])
            out[f"lb_res{r}_rev_rate"] = float(self.reversal_rate[i])
            out[f"lb_res{r}_ticks_in_line"] = float(self.line_ticks[i])
        return out

    def stats(self) -> dict:
        return {
            "engines": self.k,
            "resolutions": f"{int(self.resolutions[0])}..{int(self.resolutions[-1])}",
            "ticks": self.ticks_seen,
            "lines_total": int(self.lines.sum()),
            "reversals_total": int(self.reversals.sum()),
            "reversal_lines": self.rev_k,
        }
