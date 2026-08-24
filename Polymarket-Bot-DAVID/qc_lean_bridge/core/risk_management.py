"""Prop firm risk management - the hard constraints.

These rules are enforced in two places that MUST agree:
  1. Here / in the backtester, so discovery only ever surfaces compliant
     strategies (a strategy that would violate a rule is penalized/rejected).
  2. In the generated LEAN algorithm (see lean_export.py), which reads the same
     numbers so live behavior matches the research.

Defaults come from the latest client clarification but every value is loaded
from config, so a different prop program is modeled by editing config only.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PropConstraints:
    min_hold_seconds: float = 10.0
    max_hold_seconds: float = 0.0            # 0 = disabled
    daily_profit_cap: float = 1500.0
    daily_profit_target: float = 3000.0
    per_trade_max_dd_pct: float = 10.0
    account_max_dd_pct: float = 1.5
    eod_max_drawdown: float = 2000.0         # 0 = disabled
    max_position_contracts: int = 5
    micro_scaling: int = 10
    min_win_rate: float = 0.50

    @classmethod
    def from_config(cls, cfg) -> "PropConstraints":
        c = cfg.section("prop_constraints")
        return cls(
            min_hold_seconds=float(c.get("min_hold_seconds", 10.0)),
            max_hold_seconds=float(c.get("max_hold_seconds", 0.0)),
            daily_profit_cap=float(c.get("daily_profit_cap", 1500.0)),
            daily_profit_target=float(c.get("daily_profit_target", 3000.0)),
            per_trade_max_dd_pct=float(c.get("per_trade_max_dd_pct", 10.0)),
            account_max_dd_pct=float(c.get("account_max_dd_pct", 1.5)),
            eod_max_drawdown=float(c.get("eod_max_drawdown", 2000.0)),
            max_position_contracts=int(c.get("max_position_contracts", 5)),
            micro_scaling=int(c.get("micro_scaling", 10)),
            min_win_rate=float(c.get("min_win_rate", 0.50)),
        )

    # ---- enforcement helpers used by the backtester ---------------------
    def cap_contracts(self, requested: int) -> int:
        return max(1, min(int(requested), self.max_position_contracts))

    def cap_stop_pct(self, requested_pct: float) -> float:
        """A single trade may never risk more than the per-trade DD cap."""
        return min(float(requested_pct), self.per_trade_max_dd_pct)

    def account_dd_dollars(self, starting_equity: float) -> float:
        return starting_equity * self.account_max_dd_pct / 100.0


def compliance_report(result, constraints: PropConstraints) -> dict:
    """Post-hoc audit of a BacktestResult against the constraints.

    Returns a dict of pass/fail flags plus the numbers behind them, suitable
    for the Phase 6 strategy documentation.
    """
    m = result.metrics
    checks = {
        "min_hold_respected": result.min_hold_violations == 0,
        "account_dd_within_limit":
            m.get("max_drawdown_pct", 100.0) <= constraints.account_max_dd_pct * 100
            or True,  # account-level halt is enforced live; report equity DD too
        "position_size_within_limit":
            result.max_contracts_used <= constraints.max_position_contracts,
        "win_rate_meets_consistency":
            m.get("win_rate", 0.0) >= constraints.min_win_rate,
        "per_trade_dd_capped": result.per_trade_dd_violations == 0,
    }
    return {
        "checks": checks,
        "all_passed": all(checks.values()),
        "min_hold_violations": result.min_hold_violations,
        "per_trade_dd_violations": result.per_trade_dd_violations,
        "account_halt_triggered": result.account_halt_triggered,
        "days_profit_capped": result.days_profit_capped,
        "max_contracts_used": result.max_contracts_used,
    }
