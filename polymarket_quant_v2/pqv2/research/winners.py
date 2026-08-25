"""Winner / loser decomposition: what actually distinguishes the tails.

The objective the brief states, and it is the right one:

    SMALL LOSSES + LARGE WINNERS,  rather than maximising win percentage.

A 40% win rate with 4:1 asymmetry beats an 85% win rate with 1:9, and on this
venue the second shape is easy to build by accident: buying at 0.90 wins 90% of
the time and loses everything the rest, which is a coin flip dressed as skill.

So this module never reports a win rate without the asymmetry beside it, and it
buckets fills by OUTCOME MAGNITUDE to ask what was different at ENTRY about the
trades that became monsters versus the ones that became disasters. Only
entry-time features are compared -- comparing exit-time features would be
explaining the outcome with the outcome.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Buckets are on return-on-capital, not dollars, so a large stake cannot
# manufacture a "monster winner".
BUCKETS = [
    ("LARGE_LOSER", -10.0, -0.60),
    ("MEDIUM_LOSER", -0.60, -0.25),
    ("SMALL_LOSER", -0.25, 0.0),
    ("SMALL_WINNER", 0.0, 0.15),
    ("MEDIUM_WINNER", 0.15, 0.40),
    ("LARGE_WINNER", 0.40, 1.00),
    ("MONSTER_WINNER", 1.00, 1e9),
]

# Entry-time only. Adding an exit-time field here would make every finding
# circular.
ENTRY_FEATURES = ["entry", "stake", "rel_notional", "secs_to_settle",
                  "market_prints", "hold_secs"]


def bucket_of(ret: float) -> str:
    for name, lo, hi in BUCKETS:
        if lo <= ret < hi:
            return name
    return "MONSTER_WINNER" if ret > 0 else "LARGE_LOSER"


@dataclass
class BucketStats:
    name: str
    n: int = 0
    share: float = 0.0
    mean_return: float = 0.0
    total_pnl: float = 0.0
    pnl_share: float = 0.0
    features: dict = None

    def to_dict(self) -> dict:
        return {"bucket": self.name, "n": self.n, "share": round(self.share, 4),
                "mean_return": round(self.mean_return, 4),
                "total_pnl": round(self.total_pnl, 2),
                "pnl_share": round(self.pnl_share, 4),
                "features": {k: round(v, 4) for k, v in (self.features or {}).items()}}


def decompose(fills) -> dict:
    """Bucket the fills and describe each bucket by its ENTRY conditions."""
    if not fills:
        return {"buckets": [], "asymmetry": {}, "note": "no fills"}

    grouped: dict = {}
    for f in fills:
        grouped.setdefault(bucket_of(f.ret), []).append(f)

    total_pnl_abs = sum(abs(f.pnl) for f in fills) or 1.0
    out = []
    for name, _, _ in BUCKETS:
        members = grouped.get(name) or []
        if not members:
            out.append(BucketStats(name, 0).to_dict())
            continue
        b = BucketStats(name, len(members))
        b.share = len(members) / len(fills)
        b.mean_return = sum(f.ret for f in members) / len(members)
        b.total_pnl = sum(f.pnl for f in members)
        b.pnl_share = b.total_pnl / total_pnl_abs
        b.features = {feat: sum(getattr(f, feat, 0.0) or 0.0 for f in members)
                      / len(members) for feat in ENTRY_FEATURES}
        out.append(b.to_dict())

    rets = [f.ret for f in fills]
    wins = sorted([r for r in rets if r > 0])
    losses = sorted([r for r in rets if r <= 0])
    gross_win = sum(wins)
    gross_loss = -sum(losses)

    # How much of the total profit comes from the top 5% of trades. If this is
    # near 1.0 the strategy is a lottery: it needs the tail to survive, and any
    # rule that clips the tail destroys it.
    top = sorted(rets, reverse=True)[:max(1, len(rets) // 20)]
    tail_dependence = sum(top) / sum(rets) if sum(rets) > 0 else 0.0

    asym = {
        "n": len(rets),
        "win_rate": len(wins) / len(rets),
        "expectancy": sum(rets) / len(rets),
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "median_win": wins[len(wins) // 2] if wins else 0.0,
        "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "median_loss": losses[len(losses) // 2] if losses else 0.0,
        "largest_win": wins[-1] if wins else 0.0,
        "largest_loss": losses[0] if losses else 0.0,
        "win_loss_ratio": ((gross_win / len(wins)) / (gross_loss / len(losses)))
        if wins and losses else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else 0.0,
        "tail_dependence_top5pct": tail_dependence,
    }
    return {"buckets": out, "asymmetry": {k: round(v, 5) for k, v in asym.items()},
            "note": _interpret(asym)}


def _interpret(a: dict) -> str:
    bits = []
    if a["win_rate"] > 0.75 and a["win_loss_ratio"] < 0.4:
        bits.append(
            "high win rate carried by small wins against rare large losses - "
            "this is the shape that looks excellent until the tail arrives")
    if a["win_loss_ratio"] > 2.0:
        bits.append("winners are materially larger than losers - the asymmetry "
                    "the brief asks for is present")
    if a["tail_dependence_top5pct"] > 0.8:
        bits.append(
            f"{a['tail_dependence_top5pct']:.0%} of all profit comes from the "
            "top 5% of trades: this strategy IS its tail, so any profit target "
            "that clips winners will destroy it")
    if a["profit_factor"] < 1.0:
        bits.append("profit factor below 1: this loses money")
    return "; ".join(bits) or "no strong asymmetry signature"


def separating_features(fills, min_n: int = 15) -> list:
    """Which ENTRY features separate large winners from large losers?

    A t-test per feature between the two tails. Uncorrected p-values, reported
    as such: with 6 features tested this is a research pointer, not a finding.
    """
    winners = [f for f in fills if f.ret >= 0.40]
    losers = [f for f in fills if f.ret <= -0.60]
    if len(winners) < min_n or len(losers) < min_n:
        return [{"note": f"need {min_n} of each tail; have "
                         f"{len(winners)} winners and {len(losers)} losers"}]

    from ..validation.stats import two_sided_p
    out = []
    for feat in ENTRY_FEATURES:
        w = [float(getattr(f, feat, 0.0) or 0.0) for f in winners]
        l = [float(getattr(f, feat, 0.0) or 0.0) for f in losers]
        mw, ml = sum(w) / len(w), sum(l) / len(l)
        vw = sum((x - mw) ** 2 for x in w) / (len(w) - 1)
        vl = sum((x - ml) ** 2 for x in l) / (len(l) - 1)
        se = math.sqrt(vw / len(w) + vl / len(l))
        if se <= 0:
            continue
        t = (mw - ml) / se
        out.append({"feature": feat, "winner_mean": round(mw, 4),
                    "loser_mean": round(ml, 4), "delta": round(mw - ml, 4),
                    "t_stat": round(t, 3),
                    "p_value_uncorrected": round(two_sided_p(t), 5)})
    out.sort(key=lambda d: d["p_value_uncorrected"])
    return out
