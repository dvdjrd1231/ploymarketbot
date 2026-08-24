"""Phase 4 - Validation Framework.

Puts each discovered strategy through the rigorous robustness gauntlet the brief
requires and returns a pass/fail verdict with all the evidence:

    In-Sample / Out-of-Sample split
    Walk-Forward Analysis (sequential windows)
    Monte Carlo simulation (bootstrap resampling of trades)
    Parameter Robustness (threshold perturbation)
    Sensitivity / Stability
    Overfitting detection (IS vs OOS degradation)

Only strategies that pass every configured gate are marked `accepted`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .backtester import Backtester, Strategy, BacktestResult
from .config import Config
from .features import FeatureSet
from . import metrics as M
from .risk_management import PropConstraints


@dataclass
class ValidationReport:
    strategy: Strategy
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    full: dict = field(default_factory=dict)
    in_sample: dict = field(default_factory=dict)
    out_sample: dict = field(default_factory=dict)
    walk_forward: dict = field(default_factory=dict)
    monte_carlo: dict = field(default_factory=dict)
    robustness: dict = field(default_factory=dict)
    overfitting: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        f = self.full
        return {
            "id": self.strategy.id,
            "accepted": self.accepted,
            "trades": f.get("trades", 0),
            "net_profit": round(f.get("net_profit", 0.0), 2),
            "sharpe": round(f.get("sharpe", 0.0), 3),
            "sortino": round(f.get("sortino", 0.0), 3),
            "profit_factor": round(f.get("profit_factor", 0.0), 3),
            "win_rate": round(f.get("win_rate", 0.0), 3),
            "max_dd_pct": round(f.get("max_drawdown_pct", 0.0), 2),
            "oos_sharpe": round(self.out_sample.get("sharpe", 0.0), 3),
            "wf_profitable": round(self.walk_forward.get("pct_profitable", 0.0), 2),
            "mc_profitable": round(self.monte_carlo.get("pct_profitable", 0.0), 2),
            "param_stability": round(self.robustness.get("stability", 0.0), 3),
        }


class ValidationSuite:
    def __init__(self, feature_set: FeatureSet, config: Config,
                 constraints: PropConstraints, logger=None):
        self.fs = feature_set
        self.cfg = config
        self.constraints = constraints
        self.log = logger or (lambda m: None)
        self.v = config.section("validation")
        instr = config.section("instrument")
        acct = config.section("account")
        self.point_value = float(instr.get("point_value", 5.0))
        self.commission = float(instr.get("commission_per_contract", 0.62))
        self.starting_equity = float(acct.get("starting_equity", 50000.0))
        self.fee_per_fill = float(instr.get("fee_per_fill", 0.0))

    # -------------------------------------------------------- backtest slice
    def _bt(self, lo: int, hi: int) -> Backtester:
        return Backtester(
            features=self.fs.frame.iloc[lo:hi],
            price=self.fs.price.iloc[lo:hi],
            constraints=self.constraints,
            point_value=self.point_value,
            commission=self.commission,
            starting_equity=self.starting_equity,
            fee_per_fill=self.fee_per_fill,
        )

    # -------------------------------------------------------------- validate
    def validate(self, strat: Strategy) -> ValidationReport:
        n = len(self.fs.price)
        full_res = self._bt(0, n).run(strat)
        rep = ValidationReport(strategy=strat, accepted=False,
                               full=full_res.metrics)

        rep.in_sample, rep.out_sample = self._in_out_sample(strat, n)
        rep.walk_forward = self._walk_forward(strat, n)
        rep.monte_carlo = self._monte_carlo(full_res)
        rep.robustness = self._robustness(strat, n)
        rep.overfitting = self._overfitting(rep.in_sample, rep.out_sample)

        rep.accepted, rep.reasons = self._verdict(rep, full_res)
        return rep

    def validate_many(self, strategies: list[Strategy], progress=None) -> list[ValidationReport]:
        out = []
        total = len(strategies)
        for i, s in enumerate(strategies):
            out.append(self.validate(s))
            if progress:
                progress(i + 1, total)
        return out

    # ------------------------------------------------------------ IS / OOS
    def _in_out_sample(self, strat, n):
        frac = float(self.v.get("in_sample_fraction", 0.7))
        split = int(n * frac)
        is_res = self._bt(0, split).run(strat)
        oos_res = self._bt(split, n).run(strat)
        return is_res.metrics, oos_res.metrics

    # --------------------------------------------------------- walk forward
    def _walk_forward(self, strat, n):
        windows = int(self.v.get("walk_forward_windows", 4))
        if windows < 2 or n < windows * 10:
            return {"windows": 0, "pct_profitable": 0.0, "avg_sharpe": 0.0,
                    "efficiency": 0.0}
        bounds = np.linspace(0, n, windows + 1, dtype=int)
        sharpes, profits = [], []
        for w in range(windows):
            lo, hi = bounds[w], bounds[w + 1]
            r = self._bt(lo, hi).run(strat)
            sharpes.append(r.metrics["sharpe"])
            profits.append(1.0 if r.metrics["net_profit"] > 0 else 0.0)
        # efficiency: later windows vs first (out-of-sample realism proxy)
        first = sharpes[0] if sharpes[0] != 0 else 1e-9
        later = np.mean(sharpes[1:]) if len(sharpes) > 1 else sharpes[0]
        eff = float(np.clip(later / abs(first), 0, 3)) if first else 0.0
        return {
            "windows": windows,
            "pct_profitable": float(np.mean(profits)),
            "avg_sharpe": float(np.mean(sharpes)),
            "efficiency": eff,
            "per_window_sharpe": [round(s, 3) for s in sharpes],
        }

    # ---------------------------------------------------------- monte carlo
    def _monte_carlo(self, full_res: BacktestResult):
        runs = int(self.v.get("monte_carlo_runs", 500))
        pnl = full_res.trade_pnl
        if pnl.size < 5:
            return {"runs": 0, "pct_profitable": 0.0, "p05_net": 0.0,
                    "median_net": 0.0, "p95_max_dd": 0.0}
        rng = np.random.RandomState(12345)
        finals, dds = [], []
        for _ in range(runs):
            sample = rng.choice(pnl, size=pnl.size, replace=True)
            eq = self.starting_equity + np.cumsum(sample)
            finals.append(eq[-1] - self.starting_equity)
            peak = np.maximum.accumulate(np.concatenate([[self.starting_equity], eq]))
            dd = (peak[1:] - eq)
            dds.append(dd.max())
        finals = np.array(finals)
        dds = np.array(dds)
        return {
            "runs": runs,
            "pct_profitable": float((finals > 0).mean()),
            "p05_net": float(np.percentile(finals, 5)),
            "median_net": float(np.percentile(finals, 50)),
            "p95_max_dd": float(np.percentile(dds, 95)),
        }

    # ----------------------------------------------------------- robustness
    def _robustness(self, strat: Strategy, n: int):
        pct = float(self.v.get("robustness_perturb_pct", 10.0)) / 100.0
        base_thr = strat.entry_threshold
        variants = []
        for mult in (1 - pct, 1 - pct / 2, 1.0, 1 + pct / 2, 1 + pct):
            s2 = Strategy(**{**strat.to_dict()})
            s2.entry_threshold = base_thr * mult if base_thr != 0 else base_thr + mult - 1
            r = self._bt(0, n).run(s2)
            variants.append(r.metrics["sharpe"])
        variants = np.array(variants)
        mean_s = variants.mean()
        std_s = variants.std()
        # stability: 1 when all perturbations give similar Sharpe, ->0 when noisy
        stability = float(np.clip(1.0 - (std_s / (abs(mean_s) + 1e-6)), 0.0, 1.0))
        return {
            "perturb_pct": pct * 100,
            "sharpe_variants": [round(s, 3) for s in variants],
            "mean_sharpe": float(mean_s),
            "std_sharpe": float(std_s),
            "stability": stability,
            "all_positive": bool((variants > 0).all()),
        }

    # --------------------------------------------------------- overfitting
    def _overfitting(self, is_m, oos_m):
        is_sh = is_m.get("sharpe", 0.0)
        oos_sh = oos_m.get("sharpe", 0.0)
        if is_sh <= 0:
            degr = 1.0
        else:
            degr = float(np.clip(1.0 - oos_sh / is_sh, 0.0, 2.0))
        return {
            "is_sharpe": is_sh,
            "oos_sharpe": oos_sh,
            "degradation": degr,
            "flag_overfit": degr > float(self.v.get("max_oos_degradation", 0.5)),
        }

    # -------------------------------------------------------------- verdict
    def _verdict(self, rep: ValidationReport, full_res: BacktestResult):
        v = self.v
        c = self.constraints
        f = rep.full
        reasons = []

        if f["trades"] < int(v.get("min_trades", 30)):
            reasons.append(f"too few trades ({f['trades']} < {v.get('min_trades', 30)})")
        if f["sharpe"] < float(v.get("min_sharpe", 1.0)):
            reasons.append(f"Sharpe {f['sharpe']:.2f} < {v.get('min_sharpe', 1.0)}")
        if f["win_rate"] < float(v.get("min_win_rate", 0.5)):
            reasons.append(f"win rate {f['win_rate']:.2f} < {v.get('min_win_rate', 0.5)} "
                           f"(consistency rule)")
        if f["max_drawdown_pct"] > float(v.get("max_drawdown_pct", 25.0)):
            reasons.append(f"max DD {f['max_drawdown_pct']:.1f}% > "
                           f"{v.get('max_drawdown_pct', 25.0)}%")
        if rep.overfitting["flag_overfit"]:
            reasons.append(f"overfit: OOS Sharpe degraded "
                           f"{rep.overfitting['degradation']*100:.0f}%")
        if rep.monte_carlo.get("pct_profitable", 0.0) < \
                float(v.get("monte_carlo_min_profitable", 0.75)):
            reasons.append(f"Monte Carlo profitable "
                           f"{rep.monte_carlo.get('pct_profitable',0)*100:.0f}% < "
                           f"{float(v.get('monte_carlo_min_profitable',0.75))*100:.0f}%")
        # prop consistency / size compliance
        if full_res.max_contracts_used > c.max_position_contracts:
            reasons.append("position size exceeds prop max")

        return (len(reasons) == 0), reasons
