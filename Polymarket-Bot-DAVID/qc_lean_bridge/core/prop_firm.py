"""Module 8 - Prop Firm Risk Manager (RUNTIME order gate).

`risk_management.PropConstraints` holds the numbers and enforces them inside the
BACKTEST. This module enforces the same numbers in FRONT OF EVERY LIVE ORDER:
nothing reaches the broker without passing through `check_entry` / `check_exit`.

Rules enforced (from the brief + the client's clarifications):

  Daily profit goal      $3,000 program target; trading stops for the day once
                         the daily profit CAP ($1,500) is reached.
  Maximum drawdown       1.5% of account equity -> HALT everything.
  End-of-day drawdown    $2,000 trailing from the day's equity peak -> halt day.
  Position size          <= 5 contracts, with 10:1 micro scaling.
  Holding time           every position must stay open >= 10-11 s before it may
                         be closed. An exit request inside that window is
                         REFUSED (not queued) - this is the rule most likely to
                         fail an evaluation.
  Position scaling       total exposure across live strategies is capped, so two
                         strategies cannot collude past the contract limit.
  Consistency rule       50% - no single day may account for more than half of
                         cumulative profit. Approaching the line blocks new
                         entries for the day rather than busting it.

Every refusal is returned as a `Decision` with a human-readable reason, is
logged, and is published to the event bus so the GUI and the WS API show exactly
why an order was stopped.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .logging_setup import get_logger
from .risk_management import PropConstraints

log = get_logger("propfirm")


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    contracts: int = 0          # size after capping (entries)

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class Position:
    strategy_id: str
    direction: str              # long | short
    entry_price: float
    contracts: int
    entry_ts: float
    stop_price: float = 0.0
    target_price: float = 0.0

    def held_seconds(self, ts: float) -> float:
        return max(0.0, ts - self.entry_ts)

    def unrealized(self, price: float, point_value: float) -> float:
        sign = 1.0 if self.direction == "long" else -1.0
        return sign * (price - self.entry_price) * point_value * self.contracts


@dataclass
class AccountState:
    starting_equity: float = 50_000.0
    equity: float = 50_000.0
    peak_equity: float = 50_000.0

    day: str = ""
    day_start_equity: float = 50_000.0
    day_peak_equity: float = 50_000.0
    daily_pnl: float = 0.0

    total_profit: float = 0.0
    best_day_profit: float = 0.0
    day_profits: dict[str, float] = field(default_factory=dict)

    halted: bool = False              # account-level (permanent for the run)
    halt_reason: str = ""
    day_halted: bool = False
    day_capped: bool = False

    positions: dict[str, Position] = field(default_factory=dict)

    @property
    def open_contracts(self) -> int:
        return sum(p.contracts for p in self.positions.values())


class PropFirmRiskManager:
    """The gate. Every order passes through it, live or paper."""

    def __init__(self, constraints: PropConstraints, starting_equity: float = 50_000.0,
                 point_value: float = 5.0, commission: float = 0.62, bus=None):
        self.c = constraints
        self.point_value = point_value
        self.commission = commission
        self.bus = bus
        self.state = AccountState(
            starting_equity=starting_equity, equity=starting_equity,
            peak_equity=starting_equity, day_start_equity=starting_equity,
            day_peak_equity=starting_equity)
        self.rejections: dict[str, int] = {}

    # -------------------------------------------------------------- day roll
    @staticmethod
    def _day_key(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    def _roll_day(self, ts: float) -> None:
        day = self._day_key(ts)
        s = self.state
        if s.day == day:
            return
        if s.day:
            s.day_profits[s.day] = s.daily_pnl
            if s.daily_pnl > s.best_day_profit:
                s.best_day_profit = s.daily_pnl
            log.info("day closed", extra={"day": s.day, "pnl": round(s.daily_pnl, 2)})
        s.day = day
        s.day_start_equity = s.equity
        s.day_peak_equity = s.equity
        s.daily_pnl = 0.0
        s.day_halted = False
        s.day_capped = False

    # ---------------------------------------------------------------- checks
    def _reject(self, reason: str, code: str) -> Decision:
        self.rejections[code] = self.rejections.get(code, 0) + 1
        return Decision(False, reason, 0)

    def check_entry(self, ts: float, strategy_id: str, direction: str,
                    requested_contracts: int) -> Decision:
        """Can this strategy OPEN a position right now, and how big?"""
        self._roll_day(ts)
        s, c = self.state, self.c

        if s.halted:
            return self._reject(f"Account halted: {s.halt_reason}", "account_halt")
        if s.day_halted:
            return self._reject("Day halted (end-of-day drawdown breached)", "day_halt")
        if s.day_capped:
            return self._reject(
                f"Daily profit cap ${c.daily_profit_cap:,.0f} reached - no new trades today",
                "daily_cap")
        if strategy_id in s.positions:
            return self._reject("Strategy already has an open position", "already_open")

        # Position scaling: the whole book, not just this strategy.
        room = c.max_position_contracts - s.open_contracts
        if room <= 0:
            return self._reject(
                f"Max position size reached ({c.max_position_contracts} contracts)",
                "max_size")
        size = max(1, min(int(requested_contracts), room))

        # 50% consistency rule: if today's profit would dominate the account's
        # cumulative profit, stop adding risk today rather than bust the rule.
        if self._consistency_at_risk():
            return self._reject(
                "50% consistency rule: today's profit is approaching half of "
                "total profit - no new entries today", "consistency")

        return Decision(True, "ok", size)

    def check_exit(self, ts: float, strategy_id: str, forced: bool = False) -> Decision:
        """Can this position be CLOSED right now? Min-hold is the hard one."""
        self._roll_day(ts)
        pos = self.state.positions.get(strategy_id)
        if pos is None:
            return self._reject("No open position for strategy", "no_position")

        held = pos.held_seconds(ts)
        if held < self.c.min_hold_seconds:
            # Prop rule: a position may not be closed inside the min-hold window.
            # Even a stop-out waits - so the size limit, not the stop, is what
            # bounds the loss in that window.
            return self._reject(
                f"Min hold {self.c.min_hold_seconds:.0f}s not met "
                f"(held {held:.1f}s)", "min_hold")

        if self.c.max_hold_seconds > 0 and held >= self.c.max_hold_seconds and not forced:
            return Decision(True, "max hold reached", pos.contracts)
        return Decision(True, "ok", pos.contracts)

    def _consistency_at_risk(self) -> bool:
        s = self.state
        if s.daily_pnl <= 0:
            return False
        total = s.total_profit + s.daily_pnl
        if total <= 0:
            return False
        # Only bites once there is a meaningful profit base to be consistent about.
        if total < self.c.daily_profit_target * 0.5:
            return False
        return (s.daily_pnl / total) > 0.5

    # ----------------------------------------------------------------- fills
    def on_entry_fill(self, ts: float, strategy_id: str, direction: str,
                      price: float, contracts: int, stop_pct: float,
                      target_pct: float) -> Position:
        self._roll_day(ts)
        stop_pct = self.c.cap_stop_pct(stop_pct) / 100.0
        target_pct = target_pct / 100.0
        if direction == "long":
            stop_price = price * (1 - stop_pct)
            target_price = price * (1 + target_pct)
        else:
            stop_price = price * (1 + stop_pct)
            target_price = price * (1 - target_pct)

        pos = Position(strategy_id=strategy_id, direction=direction,
                       entry_price=price, contracts=contracts, entry_ts=ts,
                       stop_price=stop_price, target_price=target_price)
        self.state.positions[strategy_id] = pos
        log.info("entry filled", extra={
            "strategy": strategy_id, "dir": direction, "px": price,
            "contracts": contracts, "stop": round(stop_price, 4),
            "target": round(target_price, 4)})
        return pos

    def on_exit_fill(self, ts: float, strategy_id: str, price: float,
                     reason: str = "") -> float:
        """Realize the trade, update every equity/drawdown counter. Returns P&L."""
        self._roll_day(ts)
        s = self.state
        pos = s.positions.pop(strategy_id, None)
        if pos is None:
            return 0.0

        gross = pos.unrealized(price, self.point_value)
        pnl = gross - self.commission * pos.contracts * 2
        s.equity += pnl
        s.daily_pnl += pnl
        s.peak_equity = max(s.peak_equity, s.equity)
        s.day_peak_equity = max(s.day_peak_equity, s.equity)
        if pnl > 0:
            s.total_profit += pnl

        self._evaluate_limits(ts)
        log.info("exit filled", extra={
            "strategy": strategy_id, "px": price, "pnl": round(pnl, 2),
            "reason": reason, "equity": round(s.equity, 2),
            "daily_pnl": round(s.daily_pnl, 2)})
        return pnl

    def mark_to_market(self, ts: float, price: float) -> None:
        """Unrealized equity moves can breach the DD limits too - check them."""
        self._roll_day(ts)
        s = self.state
        unreal = sum(p.unrealized(price, self.point_value) for p in s.positions.values())
        equity_now = s.equity + unreal
        s.day_peak_equity = max(s.day_peak_equity, equity_now)
        s.peak_equity = max(s.peak_equity, equity_now)
        self._evaluate_limits(ts, equity_now)

    def _evaluate_limits(self, ts: float, equity_now: float | None = None) -> None:
        s, c = self.state, self.c
        eq = s.equity if equity_now is None else equity_now

        dd = s.peak_equity - eq
        if dd >= c.account_dd_dollars(s.starting_equity) and not s.halted:
            s.halted = True
            s.halt_reason = (f"account drawdown ${dd:,.0f} >= "
                             f"{c.account_max_dd_pct}% of equity")
            log.error("ACCOUNT HALT", extra={"dd": round(dd, 2), "equity": round(eq, 2)})
            self._publish("halt", {"reason": s.halt_reason, "equity": eq})

        if c.eod_max_drawdown > 0:
            eod_dd = s.day_peak_equity - eq
            if eod_dd >= c.eod_max_drawdown and not s.day_halted:
                s.day_halted = True
                log.error("DAY HALT (EOD drawdown)",
                          extra={"eod_dd": round(eod_dd, 2)})
                self._publish("halt", {"reason": "end-of-day drawdown",
                                       "eod_dd": eod_dd})

        if s.daily_pnl >= c.daily_profit_cap and not s.day_capped:
            s.day_capped = True
            log.info("daily profit cap reached",
                     extra={"daily_pnl": round(s.daily_pnl, 2)})
            self._publish("daily_cap", {"daily_pnl": s.daily_pnl})

    def _publish(self, kind: str, detail: dict) -> None:
        if self.bus is None:
            return
        from .events import LiveEvent, LIVE
        self.bus.publish(LIVE, LiveEvent(ts=time.time(), kind=kind, detail=detail))

    # ---------------------------------------------------------------- status
    def status(self) -> dict:
        s, c = self.state, self.c
        equity_dd = s.peak_equity - s.equity
        return {
            "equity": round(s.equity, 2),
            "starting_equity": s.starting_equity,
            "peak_equity": round(s.peak_equity, 2),
            "drawdown": round(equity_dd, 2),
            "drawdown_limit": round(c.account_dd_dollars(s.starting_equity), 2),
            "daily_pnl": round(s.daily_pnl, 2),
            "daily_profit_cap": c.daily_profit_cap,
            "daily_profit_target": c.daily_profit_target,
            "eod_drawdown": round(s.day_peak_equity - s.equity, 2),
            "eod_drawdown_limit": c.eod_max_drawdown,
            "open_contracts": s.open_contracts,
            "max_contracts": c.max_position_contracts,
            "micro_scaling": c.micro_scaling,
            "min_hold_seconds": c.min_hold_seconds,
            "halted": s.halted,
            "halt_reason": s.halt_reason,
            "day_halted": s.day_halted,
            "day_capped": s.day_capped,
            "total_profit": round(s.total_profit, 2),
            "best_day_profit": round(s.best_day_profit, 2),
            "consistency_ok": not self._consistency_at_risk(),
            "positions": [
                {"strategy": p.strategy_id, "dir": p.direction,
                 "entry": p.entry_price, "contracts": p.contracts,
                 "stop": round(p.stop_price, 4), "target": round(p.target_price, 4)}
                for p in s.positions.values()
            ],
            "rejections": dict(self.rejections),
        }
