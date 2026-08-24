"""Phase 5 - Strategy Ranking System.

Combines the validation evidence into a single objective score and sorts
strategies from most to least robust. Weights are configurable; the priority
order in the brief (Sharpe > Sortino > Profit Factor > Win Rate > Max DD > OOS >
Walk-Forward > Parameter Stability > Recovery Factor) is the default weighting.

Metrics where "lower is better" (drawdown, ulcer) are inverted before scoring,
and every metric is min-max normalized across the candidate pool so no single
large-magnitude metric dominates.
"""
from __future__ import annotations

import numpy as np

from .config import Config
from .validators import ValidationReport


class StrategyRanker:
    def __init__(self, config: Config):
        self.weights = config.section("ranking").get("weights", {})

    def rank(self, reports: list[ValidationReport]) -> list[ValidationReport]:
        if not reports:
            return []

        # assemble raw metric vectors
        def col(getter):
            return np.array([getter(r) for r in reports], dtype=float)

        raw = {
            "sharpe": col(lambda r: r.full.get("sharpe", 0.0)),
            "sortino": col(lambda r: r.full.get("sortino", 0.0)),
            "profit_factor": col(lambda r: min(r.full.get("profit_factor", 0.0), 10.0)),
            "win_rate": col(lambda r: r.full.get("win_rate", 0.0)),
            # inverted (lower dd better) -> negate
            "max_drawdown": col(lambda r: -r.full.get("max_drawdown_pct", 0.0)),
            "oos_performance": col(lambda r: r.out_sample.get("sharpe", 0.0)),
            "walk_forward": col(lambda r: r.walk_forward.get("pct_profitable", 0.0)),
            "parameter_stability": col(lambda r: r.robustness.get("stability", 0.0)),
            "recovery_factor": col(lambda r: min(r.full.get("recovery_factor", 0.0), 20.0)),
        }

        # min-max normalize each metric to 0..1
        norm = {}
        for key, vec in raw.items():
            lo, hi = np.nanmin(vec), np.nanmax(vec)
            norm[key] = (vec - lo) / (hi - lo) if hi > lo else np.zeros_like(vec)

        total_w = sum(self.weights.get(k, 0.0) for k in norm) or 1.0
        score = np.zeros(len(reports))
        for key, vec in norm.items():
            score += self.weights.get(key, 0.0) * vec
        score /= total_w

        for r, s in zip(reports, score):
            r.full["rank_score"] = float(s)

        order = np.argsort(-score)
        ranked = [reports[i] for i in order]
        for rank, r in enumerate(ranked, start=1):
            r.full["rank"] = rank
        return ranked
