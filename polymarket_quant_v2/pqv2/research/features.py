"""Feature evaluation, and inert-axis detection.

Rule: do not assume any feature is predictive. Measure it.

Two measurements live here, and the second one is the more useful:

  1. INFORMATION. Does conditioning on a feature change expectancy, and by
     more than the noise in the split? Reported as a lift with a t-statistic,
     never as a bare correlation.

  2. INERTNESS. Does the feature VARY at all on this substrate? A feature that
     is constant makes every rule keyed on it a no-op that still consumes a
     slot in the multiple-testing budget -- which makes the BH threshold
     stricter for no benefit and quietly suppresses real candidates.

Inertness is the one that was actually costing this project. Measured on the
client's data: `pit_evidence_share` is 0.00 for every wallet tested, because
`resolutions.settled_ts` is 0 in all 8,116 rows, so no trade has any settled
track record behind it at the moment it is placed. That makes three of the
eight search axes -- min_settled_n, min_roll_win_rate, max_consec_losses --
structurally inert. The search therefore tests 5,184 transformations per wallet
of which only 432 are distinct, and pays the multiple-testing cost of all
5,184.

The V1 engine has the same class of problem at larger scale: 47 of its
engineered feature columns are constant because no historical order book
exists. Nobody was told, so nobody could act on it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class FeatureVerdict:
    name: str
    n: int
    distinct: int
    variance: float
    inert: bool
    lift: float = 0.0
    t_stat: float = 0.0
    p_value: float = 1.0
    note: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "n": self.n, "distinct": self.distinct,
                "variance": round(self.variance, 8), "inert": self.inert,
                "lift": round(self.lift, 5), "t_stat": round(self.t_stat, 3),
                "p_value": round(self.p_value, 5), "note": self.note}


# Every observation field a strategy may condition on. Adding a rule that keys
# on a field absent from this list means its inertness is never checked.
CONDITIONABLE = [
    "price", "notional", "rel_notional", "secs_to_settle",
    "market_recent_prints", "market_price_move", "market_velocity",
    "tape_price_gap", "price_vs_wallet_norm", "hour_of_day",
    "w_settled_n", "w_win_rate", "w_roi", "w_roll_win_rate", "w_roll_roi",
    "w_edge_t", "w_consec_losses", "w_consec_wins", "w_seen_n",
    "w_secs_since_prev", "w_open_notional",
]

# Which search axes die when which feature is inert. This mapping is what turns
# "that column is constant" into "stop paying for 12x the hypotheses".
AXIS_DEPENDENCIES = {
    "w_settled_n": ["min_settled_n"],
    "w_roll_win_rate": ["min_roll_win_rate"],
    "w_consec_losses": ["max_consec_losses"],
    "w_edge_t": ["min_edge_t"],
    "market_recent_prints": ["min_market_prints"],
    "market_price_move": ["max_market_move", "min_market_move"],
    "rel_notional": ["min_rel_notional"],
    "price": ["price_band"],
}


def _values(observations, name: str) -> list:
    out = []
    for o in observations:
        v = getattr(o, name, None)
        if v is None:
            continue
        out.append(float(v))
    return out


def evaluate_feature(observations, name: str,
                     min_distinct: int = 3) -> FeatureVerdict:
    """Is this feature informative, and does it vary at all?"""
    vals = _values(observations, name)
    n = len(vals)
    if n == 0:
        return FeatureVerdict(name, 0, 0, 0.0, True,
                              note="not present on these observations")
    distinct = len(set(vals))
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1) if n > 1 else 0.0
    inert = distinct < min_distinct or var <= 1e-12
    v = FeatureVerdict(name, n, distinct, var, inert)
    if inert:
        v.note = (f"constant at {mean:g} across all {n} observations - every "
                  "rule keyed on it is a no-op that still costs a hypothesis")
        return v

    # Information: split at the median and compare hold-to-resolution returns.
    # Median split rather than a fitted threshold: fitting the threshold on the
    # same data would manufacture the lift being measured.
    pairs = [(float(getattr(o, name)), o.trade.gross_return())
             for o in observations if getattr(o, name, None) is not None]
    pairs.sort(key=lambda t: t[0])
    mid = len(pairs) // 2
    lo = [r for _, r in pairs[:mid]]
    hi = [r for _, r in pairs[mid:]]
    if len(lo) < 10 or len(hi) < 10:
        v.note = "too few observations either side of the median to test"
        return v
    m_lo, m_hi = sum(lo) / len(lo), sum(hi) / len(hi)
    var_lo = sum((r - m_lo) ** 2 for r in lo) / (len(lo) - 1)
    var_hi = sum((r - m_hi) ** 2 for r in hi) / (len(hi) - 1)
    se = math.sqrt(var_lo / len(lo) + var_hi / len(hi))
    v.lift = m_hi - m_lo
    v.t_stat = (v.lift / se) if se > 0 else 0.0
    from ..validation.stats import two_sided_p
    v.p_value = two_sided_p(v.t_stat)
    return v


def audit_features(observations, names=None) -> dict:
    """Full feature audit, plus the axes it invalidates."""
    names = names or CONDITIONABLE
    verdicts = [evaluate_feature(observations, n) for n in names]
    inert = [v.name for v in verdicts if v.inert]
    dead_axes = sorted({a for f in inert for a in AXIS_DEPENDENCIES.get(f, [])})

    from ..strategy_b.strategy import AXES
    live = 1
    full = 1
    for axis, values in AXES.items():
        full *= len(values)
        live *= 1 if axis in dead_axes else len(values)

    return {
        "features": [v.to_dict() for v in sorted(
            verdicts, key=lambda v: (v.inert, v.p_value))],
        "inert_features": inert,
        "dead_axes": dead_axes,
        "grid_nominal": full,
        "grid_effective": live,
        "wasted_multiple_testing_factor": round(full / live, 1) if live else 0.0,
        "note": (
            f"{len(dead_axes)} of {len(AXES)} search axes are inert on this "
            f"substrate. The sweep tests {full:,} transformations per wallet of "
            f"which {live:,} are distinct, and pays the multiple-testing cost "
            f"of all {full:,}. Disabling the dead axes would loosen the BH "
            f"threshold by ~{full / live:.0f}x at zero cost in coverage."
        ) if dead_axes else "all search axes vary on this substrate",
    }


def informative(observations, names=None, alpha: float = 0.05) -> list:
    """The features that actually carry information, best first.

    Note the p-values here are UNCORRECTED for the number of features tested.
    With ~21 features and alpha 0.05, one false positive is expected. Use this
    to prioritise research, never to promote a strategy -- promotion goes
    through the pass-wide BH threshold in validate.py.
    """
    verdicts = [evaluate_feature(observations, n) for n in (names or CONDITIONABLE)]
    hits = [v for v in verdicts if not v.inert and v.p_value <= alpha]
    hits.sort(key=lambda v: v.p_value)
    return [v.to_dict() for v in hits]
