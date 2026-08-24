"""Custom backtesting harness with prop-firm constraints baked in.

Why a custom engine instead of running LEAN directly for discovery?
  * We need to test *thousands* of rule combinations quickly and run
    walk-forward / Monte Carlo loops on top. Spinning up LEAN per candidate is
    far too slow. This vectorized-entry + event-driven-exit engine backtests a
    candidate in milliseconds.
  * The winning strategies are then EXPORTED as real LEAN code (lean_export.py)
    for the client to run/validate inside QuantConnect, so nothing is lost.

The engine models one instrument (futures) and the prop constraints:
  min hold time, per-trade DD cap, account DD halt, daily profit cap, EOD
  trailing DD, and max position size.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

from . import metrics as M
from .risk_management import PropConstraints


# --------------------------------------------------------------------------
@dataclass
class Strategy:
    """A single discovered trading rule set."""
    id: str
    direction: str                 # "long" | "short"
    entry_feature: str
    entry_op: str                  # ">" | "<"
    entry_threshold: float
    stop_pct: float                # % adverse price move (per-trade risk)
    target_pct: float              # % favorable price move (profit target)
    time_exit_bars: int = 0        # 0 = disabled
    contracts: int = 1
    exit_feature: str | None = None
    exit_op: str | None = None
    exit_threshold: float | None = None
    filter_feature: str | None = None
    filter_op: str | None = None
    filter_threshold: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        d = "LONG" if self.direction == "long" else "SHORT"
        s = (f"{d} when {self.entry_feature} {self.entry_op} "
             f"{self.entry_threshold:.4g}")
        if self.filter_feature:
            s += (f"  [filter: {self.filter_feature} {self.filter_op} "
                  f"{self.filter_threshold:.4g}]")
        s += f"; stop {self.stop_pct:.2g}%, target {self.target_pct:.2g}%"
        if self.time_exit_bars:
            s += f", time-exit {self.time_exit_bars} bars"
        if self.exit_feature:
            s += f", signal-exit {self.exit_feature} {self.exit_op} {self.exit_threshold:.4g}"
        s += f"; size {self.contracts}"
        return s


@dataclass
class BacktestResult:
    strategy: Strategy
    metrics: dict
    trades: pd.DataFrame
    equity: pd.Series
    trade_pnl: np.ndarray
    min_hold_violations: int = 0
    per_trade_dd_violations: int = 0
    account_halt_triggered: bool = False
    days_profit_capped: int = 0
    max_contracts_used: int = 0
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
def _bool_signal(values: np.ndarray, op: str, thr: float) -> np.ndarray:
    if op == ">":
        return values > thr
    if op == "<":
        return values < thr
    if op == ">=":
        return values >= thr
    if op == "<=":
        return values <= thr
    raise ValueError(f"bad operator {op}")


class Backtester:
    def __init__(self, features: pd.DataFrame, price: pd.Series,
                 constraints: PropConstraints, point_value: float = 5.0,
                 commission: float = 0.62, starting_equity: float = 50000.0,
                 fee_per_fill: float = 0.0):
        self.features = features
        self.price = price.values.astype(float)
        self.index = price.index
        self.constraints = constraints
        self.point_value = point_value
        self.commission = commission
        self.starting_equity = starting_equity
        # A cost that does NOT scale with size. `commission` is per contract,
        # which is right for a futures broker and for crossing a spread, but a
        # flat per-fill exchange fee charged that way is scale-invariant as a
        # PERCENTAGE - so it looks identical on a $50,000 account and on a
        # $100 one, and the small account's real penalty disappears. Charged
        # once per fill, it is what makes a $3 position visibly more expensive
        # to trade than a $600 one.
        self.fee_per_fill = fee_per_fill

        # Pre-compute time deltas (seconds) between bars for min-hold logic.
        if isinstance(self.index, pd.DatetimeIndex):
            # NOTE: do NOT use `.asi8 / 1e9`. In pandas 2.x a DatetimeIndex keeps
            # its OWN resolution, and parsing our timestamps yields
            # datetime64[us] - so asi8 is MICROseconds and dividing by 1e9 makes
            # every hold time 1000x too short. The 10 s min-hold then reads as
            # 10 ms, no position can ever satisfy an exit, and the backtest
            # silently produces zero trades. Normalize to ns explicitly.
            ns = self.index.to_numpy(dtype="datetime64[ns]").astype(np.int64)
            self.ts = ns.astype(np.float64) / 1e9                  # seconds
            self.day = (ns // 86_400_000_000_000).astype(np.int64)  # days since epoch
        else:
            # No real timestamps: assume each bar >= min_hold apart so hold is met.
            step = max(constraints.min_hold_seconds, 1.0)
            self.ts = np.arange(len(self.price), dtype=np.float64) * step
            self.day = (self.ts // 86400).astype(np.int64)

    # ----------------------------------------------------------------- run
    def run(self, strat: Strategy) -> BacktestResult:
        c = self.constraints
        px = self.price
        n = len(px)
        feat = self.features

        if strat.entry_feature not in feat.columns:
            return self._empty(strat)

        entry_sig = _bool_signal(feat[strat.entry_feature].values,
                                 strat.entry_op, strat.entry_threshold)
        if strat.filter_feature and strat.filter_feature in feat.columns:
            filt = _bool_signal(feat[strat.filter_feature].values,
                                strat.filter_op, strat.filter_threshold)
            entry_sig = entry_sig & filt
        if strat.exit_feature and strat.exit_feature in feat.columns:
            exit_sig = _bool_signal(feat[strat.exit_feature].values,
                                    strat.exit_op, strat.exit_threshold)
        else:
            exit_sig = np.zeros(n, dtype=bool)

        dir_sign = 1.0 if strat.direction == "long" else -1.0
        contracts = c.cap_contracts(strat.contracts)
        stop_pct = c.cap_stop_pct(strat.stop_pct) / 100.0
        target_pct = strat.target_pct / 100.0
        pv = self.point_value

        equity = self.starting_equity
        peak_equity = equity
        account_dd_limit = c.account_dd_dollars(self.starting_equity)

        equity_curve = np.empty(n, dtype=float)
        trade_records = []
        trade_pnls = []

        in_pos = False
        entry_i = 0
        entry_price = 0.0

        # daily state
        cur_day = self.day[0] if n else 0
        daily_pnl = 0.0
        day_peak_equity = equity
        daily_capped = False
        day_halted = False
        days_capped = 0

        account_halted = False
        min_hold_viol = 0
        per_trade_dd_viol = 0
        max_contracts_used = 0

        min_hold = c.min_hold_seconds
        max_hold = c.max_hold_seconds
        eod_dd = c.eod_max_drawdown

        for i in range(n):
            price_i = px[i]

            # ---- day rollover
            if self.day[i] != cur_day:
                cur_day = self.day[i]
                daily_pnl = 0.0
                day_peak_equity = equity
                daily_capped = False
                day_halted = False
            day_peak_equity = max(day_peak_equity, equity)

            # ---- manage open position
            if in_pos:
                held = self.ts[i] - self.ts[entry_i]
                bars_held = i - entry_i

                if dir_sign > 0:
                    stop_price = entry_price * (1 - stop_pct)
                    target_price = entry_price * (1 + target_pct)
                    hit_stop = price_i <= stop_price
                    hit_target = price_i >= target_price
                else:
                    stop_price = entry_price * (1 + stop_pct)
                    target_price = entry_price * (1 - target_pct)
                    hit_stop = price_i >= stop_price
                    hit_target = price_i <= target_price

                time_exit = strat.time_exit_bars > 0 and bars_held >= strat.time_exit_bars
                max_hold_hit = max_hold > 0 and held >= max_hold
                signal_exit = exit_sig[i]
                want_exit = hit_stop or hit_target or time_exit or signal_exit or max_hold_hit
                # account/eod protection forces exit intent
                force_exit = account_halted or (eod_dd > 0 and
                                                (day_peak_equity - equity) >= eod_dd)

                allowed = held >= min_hold
                if (want_exit or force_exit):
                    if not allowed:
                        # prop rule: cannot close before min hold. We hold on and
                        # count that the desired exit was delayed (not a violation
                        # of the rule - the rule is that we DON'T exit early).
                        pass
                    else:
                        exit_price = price_i
                        # exit exactly at stop/target level when that triggered
                        if hit_stop:
                            exit_price = stop_price
                        elif hit_target:
                            exit_price = target_price
                        gross = dir_sign * (exit_price - entry_price) * pv * contracts
                        cost = (self.commission * contracts * 2
                                + self.fee_per_fill * 2)
                        pnl = gross - cost
                        equity += pnl
                        daily_pnl += pnl
                        peak_equity = max(peak_equity, equity)

                        # per-trade DD audit (loss shouldn't exceed cap)
                        trade_risk = entry_price * pv * contracts
                        if pnl < 0 and trade_risk > 0 and \
                                (-pnl / trade_risk) > (c.per_trade_max_dd_pct / 100.0) + 1e-6:
                            per_trade_dd_viol += 1

                        trade_records.append({
                            "entry_time": self.index[entry_i],
                            "exit_time": self.index[i],
                            "direction": strat.direction,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "contracts": contracts,
                            "held_seconds": held,
                            "bars_held": bars_held,
                            "pnl": pnl,
                            "reason": ("stop" if hit_stop else "target" if hit_target
                                       else "time" if time_exit else "signal" if signal_exit
                                       else "force"),
                        })
                        trade_pnls.append(pnl)
                        in_pos = False

            # ---- update account halt / daily cap flags
            if (peak_equity - equity) >= account_dd_limit:
                account_halted = True
            if eod_dd > 0 and (day_peak_equity - equity) >= eod_dd:
                day_halted = True
            if daily_pnl >= c.daily_profit_cap and not daily_capped:
                daily_capped = True
                days_capped += 1

            # ---- consider new entry (flat only)
            if not in_pos and not account_halted and not daily_capped and not day_halted:
                if entry_sig[i] and i < n - 1:   # don't open on the very last bar
                    in_pos = True
                    entry_i = i
                    entry_price = price_i
                    max_contracts_used = max(max_contracts_used, contracts)

            equity_curve[i] = equity

        # close any dangling position at last price
        if in_pos and n:
            i = n - 1
            held = self.ts[i] - self.ts[entry_i]
            if held < min_hold:
                pass  # would violate min-hold to close; leave open (no realized pnl)
            else:
                exit_price = px[i]
                gross = dir_sign * (exit_price - entry_price) * pv * contracts
                pnl = gross - (self.commission * contracts * 2
                               + self.fee_per_fill * 2)
                equity += pnl
                trade_records.append({
                    "entry_time": self.index[entry_i], "exit_time": self.index[i],
                    "direction": strat.direction, "entry_price": entry_price,
                    "exit_price": exit_price, "contracts": contracts,
                    "held_seconds": held, "bars_held": i - entry_i,
                    "pnl": pnl, "reason": "eod_close",
                })
                trade_pnls.append(pnl)
                equity_curve[-1] = equity

        trades_df = pd.DataFrame(trade_records)
        pnl_arr = np.asarray(trade_pnls, dtype=float)
        stats = M.compute_all(pnl_arr, equity_curve, self.starting_equity)

        return BacktestResult(
            strategy=strat,
            metrics=stats,
            trades=trades_df,
            equity=pd.Series(equity_curve, index=self.index),
            trade_pnl=pnl_arr,
            min_hold_violations=min_hold_viol,
            per_trade_dd_violations=per_trade_dd_viol,
            account_halt_triggered=account_halted,
            days_profit_capped=days_capped,
            max_contracts_used=max_contracts_used,
        )

    # -------------------------------------------------------------- helper
    def _empty(self, strat: Strategy) -> BacktestResult:
        eq = np.full(len(self.price), self.starting_equity)
        return BacktestResult(
            strategy=strat,
            metrics=M.compute_all(np.array([]), eq, self.starting_equity),
            trades=pd.DataFrame(),
            equity=pd.Series(eq, index=self.index),
            trade_pnl=np.array([]),
        )
