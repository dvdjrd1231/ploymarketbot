"""Module 7 - Live Strategy Manager.

Deploys APPROVED strategies, runs them against the live structural stream,
monitors them, and swaps them out the moment they decay:

    deploy  -> monitor (drawdown / win rate / expectancy / degradation)
            -> disable the deteriorating strategy
            -> promote the next validated strategy in the registry
            -> tell OpenClaw to retrain

------------------------------------------------------------------------------
THE HARD PART: live features must be IDENTICAL to research features
------------------------------------------------------------------------------
A discovered rule says e.g. `LONG when bid_void_persistence_z > 1.83`. That
threshold is a quantile of a feature produced by `core.features.FeatureEngineer`
over the RESEARCH frame. If live recomputed that feature even slightly
differently, the threshold would mean something else and the live strategy would
not be the strategy that was validated.

So live does not re-implement anything. It:
  1. stores the dataset TEMPLATE (column names, metric list, bid/ask roles) at
     deploy time, in the registry, alongside the strategy;
  2. keeps a rolling buffer of the newest research rows;
  3. rebuilds a `Dataset` with that exact template and runs the SAME
     `FeatureEngineer` over it;
  4. evaluates the rule on the last row.

Same code, same column names, same transforms - so the live signal is the
researched signal.

Two cadences, on purpose:
  * every TICK  -> position management (stop / target / min-hold / mark-to-market).
                   Stops must be exact, so they are never throttled.
  * every ROW   -> entry signals, throttled to `live.eval_interval_s`, because
                   rebuilding the feature frame is the expensive part and a
                   strategy with a 10 s min-hold does not need sub-second entries.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .backtester import Strategy
from .data_pipeline import Dataset
from .events import EventBus, LiveEvent, FEATURE, TICK, LIVE
from .features import FeatureEngineer
from .logging_setup import get_logger
from .prop_firm import PropFirmRiskManager
from .risk_management import PropConstraints
from .strategy_registry import StrategyRegistry, DEPLOYED, RETIRED, VALIDATED

log = get_logger("live")


@dataclass
class LivePerf:
    """Realized live performance of ONE deployed strategy."""
    strategy_id: str
    trades: int = 0
    wins: int = 0
    net_pnl: float = 0.0
    peak_pnl: float = 0.0
    max_dd: float = 0.0
    pnls: deque = field(default_factory=lambda: deque(maxlen=200))

    # validated baseline this strategy is being held to
    base_win_rate: float = 0.0
    base_expectancy: float = 0.0

    def record(self, pnl: float) -> None:
        self.trades += 1
        self.wins += 1 if pnl > 0 else 0
        self.net_pnl += pnl
        self.pnls.append(pnl)
        self.peak_pnl = max(self.peak_pnl, self.net_pnl)
        self.max_dd = max(self.max_dd, self.peak_pnl - self.net_pnl)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def expectancy(self) -> float:
        return float(np.mean(self.pnls)) if self.pnls else 0.0

    def degradation(self) -> float:
        """0 = performing as validated, 1 = fully broken.

        Measured on the two metrics a prop account actually dies from: hit rate
        and expectancy. Relative decline, so it works for any strategy scale.
        """
        if self.trades == 0:
            return 0.0
        wr_base = max(self.base_win_rate, 1e-9)
        wr_drop = max(0.0, (wr_base - self.win_rate) / wr_base)
        ex_base = abs(self.base_expectancy) or 1e-9
        ex_drop = max(0.0, (self.base_expectancy - self.expectancy) / ex_base)
        return float(min(1.0, max(wr_drop, ex_drop)))

    def as_dict(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "trades": self.trades,
            "wins": self.wins,
            "win_rate": round(self.win_rate, 4),
            "net_pnl": round(self.net_pnl, 2),
            "expectancy": round(self.expectancy, 2),
            "max_dd": round(self.max_dd, 2),
            "degradation": round(self.degradation(), 4),
            "base_win_rate": round(self.base_win_rate, 4),
            "base_expectancy": round(self.base_expectancy, 2),
        }


class LiveStrategyManager:
    """Runs deployed strategies against the live event stream."""

    def __init__(self, cfg, registry: StrategyRegistry, bus: EventBus,
                 logger=None, on_degradation=None):
        self.cfg = cfg
        self.registry = registry
        self.bus = bus
        self.log = logger or (lambda m: None)
        self.on_degradation = on_degradation      # OpenClaw's retrain trigger

        lv = cfg.section("live")
        self.max_live = int(lv.get("max_live_strategies", 1))
        self.buffer_rows = int(lv.get("buffer_rows", 400))
        self.eval_interval = float(lv.get("eval_interval_s", 1.0))
        self.degradation_threshold = float(lv.get("degradation_threshold", 0.40))
        self.min_trades_before_judging = int(lv.get("min_trades_before_judging", 10))
        self.max_live_dd = float(lv.get("max_live_drawdown", 1000.0))
        self.auto_replace = bool(lv.get("auto_replace", True))
        self.paper = bool(lv.get("paper_trading", True))

        constraints = PropConstraints.from_config(cfg)
        self.risk = PropFirmRiskManager(
            constraints,
            starting_equity=float(cfg.get("account.starting_equity", 50000.0)),
            point_value=float(cfg.get("instrument.point_value", 5.0)),
            commission=float(cfg.get("instrument.commission_per_contract", 0.62)),
            bus=bus)

        self.strategies: dict[str, Strategy] = {}
        self.perf: dict[str, LivePerf] = {}
        self.template: dict = {}
        self.rows: deque[dict] = deque(maxlen=self.buffer_rows)

        self._last_eval = 0.0
        self._last_price = 0.0
        self._running = False
        self._feature_frame: pd.DataFrame | None = None
        self.signals = 0
        self.rejections = 0

    # ------------------------------------------------------------- deploy
    def deploy(self, record) -> bool:
        """Put an approved strategy live (module 7: 'deploy only approved')."""
        if record.status == RETIRED:
            self.log(f"[Live] refusing to deploy retired strategy {record.strategy_id}")
            return False
        if len(self.strategies) >= self.max_live:
            self.log(f"[Live] at capacity ({self.max_live}); not deploying "
                     f"{record.strategy_id}")
            return False

        d = record.strategy_dict()
        if not d:
            self.log(f"[Live] {record.strategy_id} has no rule payload; skipped")
            return False

        strat = Strategy(**{k: v for k, v in d.items()
                            if k in Strategy.__dataclass_fields__})
        self.strategies[record.strategy_id] = strat
        self.perf[record.strategy_id] = LivePerf(
            strategy_id=record.strategy_id,
            base_win_rate=record.win_rate,
            base_expectancy=record.expectancy)
        # The template travels WITH the strategy - that is what makes the live
        # feature frame identical to the researched one.
        self.template = record.dataset_template() or self.template
        self.registry.set_status(record.strategy_id, DEPLOYED, "deployed live")

        self.log(f"[Live] DEPLOYED {record.strategy_id}: {record.rule}")
        log.info("strategy deployed", extra={
            "strategy": record.strategy_id, "score": record.score,
            "base_win_rate": record.win_rate})
        self._emit("deploy", record.strategy_id,
                   {"rule": record.rule, "score": record.score})
        return True

    def disable(self, strategy_id: str, reason: str) -> None:
        self.strategies.pop(strategy_id, None)
        perf = self.perf.get(strategy_id)
        if strategy_id in self.risk.state.positions:
            # Flatten what we can; min-hold may keep it open a moment longer.
            self._try_exit(time.time(), strategy_id, self._last_price, "disable")
        self.registry.set_status(strategy_id, RETIRED, reason)
        if perf:
            self.registry.record_live(strategy_id, {**perf.as_dict(), "healthy": False})
        self.log(f"[Live] DISABLED {strategy_id} - {reason}")
        log.warning("strategy disabled", extra={"strategy": strategy_id,
                                                "reason": reason})
        self._emit("disable", strategy_id, {"reason": reason})

    def promote_next(self) -> bool:
        """Activate the strongest validated strategy that is not already live."""
        exclude = set(self.strategies) | {
            r.strategy_id for r in self.registry.by_status(RETIRED)}
        nxt = self.registry.best_candidate(exclude=exclude)
        if nxt is None:
            self.log("[Live] no validated replacement available - retraining needed")
            return False
        return self.deploy(nxt)

    # -------------------------------------------------------------- run loop
    async def run(self) -> None:
        """Consume the bus until stopped. Ticks manage positions, rows signal."""
        self._running = True
        sub = self.bus.subscribe(FEATURE, TICK, LIVE)
        self.log(f"[Live] manager running (paper={self.paper}, "
                 f"{len(self.strategies)} strategies live)")
        try:
            while self._running:
                try:
                    topic, evt = await asyncio.wait_for(sub.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if topic == TICK:
                    self._on_tick(evt)
                elif topic == FEATURE:
                    self._on_row(evt)
        finally:
            sub.close()
            self.log("[Live] manager stopped")

    def stop(self) -> None:
        self._running = False

    # ---------------------------------------------------------------- ticks
    def _on_tick(self, tick) -> None:
        """Position management. Never throttled - stops have to be exact."""
        self._last_price = tick.price
        self.risk.mark_to_market(tick.ts, tick.price)

        for sid in list(self.risk.state.positions):
            pos = self.risk.state.positions.get(sid)
            strat = self.strategies.get(sid)
            if pos is None or strat is None:
                continue

            hit_stop = (tick.price <= pos.stop_price if pos.direction == "long"
                        else tick.price >= pos.stop_price)
            hit_target = (tick.price >= pos.target_price if pos.direction == "long"
                          else tick.price <= pos.target_price)
            max_hold = (self.risk.c.max_hold_seconds > 0 and
                        pos.held_seconds(tick.ts) >= self.risk.c.max_hold_seconds)

            if hit_stop or hit_target or max_hold:
                reason = "stop" if hit_stop else "target" if hit_target else "max_hold"
                self._try_exit(tick.ts, sid, tick.price, reason)

        if self.risk.state.halted:
            for sid in list(self.strategies):
                self.disable(sid, f"account halt: {self.risk.state.halt_reason}")

    def _try_exit(self, ts: float, sid: str, price: float, reason: str) -> None:
        gate = self.risk.check_exit(ts, sid)
        if not gate.allowed:
            # Almost always the 10 s min-hold. We simply retry on the next tick.
            if "Min hold" not in gate.reason:
                log.debug("exit blocked", extra={"strategy": sid, "why": gate.reason})
            return
        pnl = self.risk.on_exit_fill(ts, sid, price, reason)
        perf = self.perf.get(sid)
        if perf:
            perf.record(pnl)
            self.registry.record_live(sid, {**perf.as_dict(), "healthy": True})
        self._emit("fill", sid, {"side": "exit", "price": price,
                                 "pnl": round(pnl, 2), "reason": reason})
        self._check_health(sid)

    # ----------------------------------------------------------------- rows
    def _on_row(self, snap) -> None:
        row = {"ts": snap.ts, **snap.values}
        self.rows.append(row)

        now = time.monotonic()
        if now - self._last_eval < self.eval_interval:
            return
        if len(self.rows) < 60:            # need history for the rolling features
            return
        self._last_eval = now

        try:
            frame = self._build_features()
        except Exception as exc:  # noqa: BLE001
            log.warning("live feature build failed", extra={"err": str(exc)})
            return
        if frame is None or frame.empty:
            return
        self._feature_frame = frame
        last = frame.iloc[-1]

        for sid, strat in list(self.strategies.items()):
            if sid in self.risk.state.positions:
                self._maybe_signal_exit(snap, strat, sid, last)
                continue
            if strat.entry_feature not in frame.columns:
                continue
            if not self._passes(last, strat.entry_feature, strat.entry_op,
                                strat.entry_threshold):
                continue
            if strat.filter_feature and not self._passes(
                    last, strat.filter_feature, strat.filter_op,
                    strat.filter_threshold):
                continue

            self.signals += 1
            gate = self.risk.check_entry(snap.ts, sid, strat.direction, strat.contracts)
            if not gate.allowed:
                self.rejections += 1
                self._emit("reject", sid, {"reason": gate.reason})
                continue

            self.risk.on_entry_fill(snap.ts, sid, strat.direction, snap.price,
                                    gate.contracts, strat.stop_pct, strat.target_pct)
            self._emit("fill", sid, {"side": "entry", "price": snap.price,
                                     "contracts": gate.contracts,
                                     "direction": strat.direction})

    def _maybe_signal_exit(self, snap, strat: Strategy, sid: str, last) -> None:
        if not strat.exit_feature or strat.exit_feature not in last.index:
            return
        if self._passes(last, strat.exit_feature, strat.exit_op, strat.exit_threshold):
            self._try_exit(snap.ts, sid, snap.price, "signal")

    @staticmethod
    def _passes(row, feature: str, op: str, threshold: float) -> bool:
        if feature not in row.index:
            return False
        v = float(row[feature])
        if not np.isfinite(v):
            return False
        if op == ">":
            return v > threshold
        if op == "<":
            return v < threshold
        if op == ">=":
            return v >= threshold
        if op == "<=":
            return v <= threshold
        return False

    # ------------------------------------------------- research-identical features
    def _build_features(self) -> pd.DataFrame | None:
        """Rebuild the RESEARCH feature frame over the live buffer.

        The template's column names may be file-prefixed (e.g.
        `bid_nonprint_structure__bid_velocity`) because the bridge prefixes each
        source CSV. The live rows are unprefixed, so we map by the suffix.
        """
        if not self.rows:
            return None
        columns = self.template.get("columns") or []
        metrics = self.template.get("metrics") or []
        meta = self.template.get("meta") or {}

        rows = list(self.rows)
        idx = pd.to_datetime([r["ts"] for r in rows], unit="s")

        if columns:
            data = {}
            for col in columns:
                key = col.split("__")[-1]
                data[col] = [float(r.get(key, np.nan)) for r in rows]
            frame = pd.DataFrame(data, index=idx)
        else:
            # No template (e.g. deployed straight from a fresh run): use whatever
            # the engines are producing right now.
            keys = [k for k in rows[-1] if k != "ts"]
            frame = pd.DataFrame(
                {k: [float(r.get(k, np.nan)) for r in rows] for k in keys}, index=idx)
            metrics = [k for k in keys if k != "price"]
            meta = {m: ("bid" if m.startswith("bid") else
                        "ask" if m.startswith("ask") else "metric") for m in metrics}

        frame = frame.ffill().bfill().fillna(0.0)
        price_key = self.template.get("price_column") or "price"
        price_col = next((c for c in frame.columns
                          if c.split("__")[-1] == price_key), None)
        price = (frame[price_col] if price_col
                 else pd.Series([float(r.get("price", 0.0)) for r in rows], index=idx))

        metrics = [m for m in metrics if m in frame.columns]
        if not metrics:
            return None

        ds = Dataset(frame=frame, price=price.astype(float), metrics=metrics,
                     meta={k: v for k, v in meta.items() if k in frame.columns})
        return FeatureEngineer(ds).build().frame

    # ------------------------------------------------------------- health
    def _check_health(self, sid: str) -> None:
        """Degradation detection -> disable, replace, and ask for a retrain."""
        perf = self.perf.get(sid)
        if perf is None or perf.trades < self.min_trades_before_judging:
            return

        deg = perf.degradation()
        reasons = []
        if deg >= self.degradation_threshold:
            reasons.append(f"degradation {deg:.0%} >= {self.degradation_threshold:.0%}")
        if perf.max_dd >= self.max_live_dd > 0:
            reasons.append(f"live drawdown ${perf.max_dd:,.0f} >= ${self.max_live_dd:,.0f}")
        if perf.win_rate < self.risk.c.min_win_rate:
            reasons.append(f"win rate {perf.win_rate:.0%} below consistency floor "
                           f"{self.risk.c.min_win_rate:.0%}")
        if not reasons:
            return

        reason = "; ".join(reasons)
        self.registry.record_live(sid, {**perf.as_dict(), "healthy": False})
        self._emit("degradation", sid, {"reason": reason, **perf.as_dict()})
        self.disable(sid, reason)

        replaced = self.promote_next() if self.auto_replace else False
        if self.on_degradation:
            # OpenClaw decides whether to retrain; it always wants to know.
            try:
                self.on_degradation(sid, reason, replaced)
            except Exception as exc:  # noqa: BLE001
                log.warning("degradation callback failed", extra={"err": str(exc)})

    # -------------------------------------------------------------- status
    def _emit(self, kind: str, sid: str, detail: dict) -> None:
        self.bus.publish(LIVE, LiveEvent(ts=time.time(), kind=kind,
                                         strategy_id=sid, detail=detail))

    def status(self) -> dict:
        return {
            "running": self._running,
            "paper": self.paper,
            "live_strategies": [
                {"id": sid, "rule": s.describe(), **self.perf[sid].as_dict()}
                for sid, s in self.strategies.items() if sid in self.perf
            ],
            "signals": self.signals,
            "rejected_orders": self.rejections,
            "rows_buffered": len(self.rows),
            "account": self.risk.status(),
        }
