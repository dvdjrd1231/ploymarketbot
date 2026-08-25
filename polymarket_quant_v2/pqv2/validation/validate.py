"""The status ladder: the only authority on whether a strategy may trade.

Nothing in this engine may promote a strategy for any reason other than
evidence. Not because it resembles RN1, not because it made money once, not
because it has many trades, not because an AI liked it, not because it survived
an attack. `tests/test_ladder.py` asserts by AST inspection that no module
outside this one calls `assign_status`.

The ladder, in evaluation order -- cheapest disqualifier first, because a
candidate with 4 fills should not cost a bootstrap:

    INSUFFICIENT_EVIDENCE  < min_oos_fills out-of-sample fills
    UNPRICEABLE            fill rate too low to be a real strategy
    FAILED                 negative out-of-sample expectancy
    NOT_SIGNIFICANT        did not clear the pass's BH threshold
    NO_WALLET_ALPHA        real, but it is market structure, not the wallet
    CONCENTRATED           > max_concentration of profit from one market
    UNSTABLE               positive in < half of walk-forward folds
    FRAGILE                fails parameter perturbation or block bootstrap
    DRIFT                  beats nothing that random entries would not
    VALIDATED              survived all of the above

VALIDATED authorises PAPER. Going LIVE is a separate human decision and this
module never makes it.

Note the deliberate asymmetry with V1: V1 required closed live trades before a
setup could be trusted, and could not get closed trades without being trusted.
That deadlock produced 40,820 DO_NOTHING decisions. Here the evidence that
promotes a strategy is out-of-sample HISTORICAL evidence, which exists today --
so the ladder can actually be climbed without loosening anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..config import Settings
from ..substrate.data import PriceTape
from ..strategy_b.strategy import CopyStrategy
from . import backtest, stats
from .baseline import BaselineBook

INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
UNPRICEABLE = "UNPRICEABLE"
FAILED = "FAILED"
NOT_SIGNIFICANT = "NOT_SIGNIFICANT"
NO_WALLET_ALPHA = "NO_WALLET_ALPHA"
CONCENTRATED = "CONCENTRATED"
UNSTABLE = "UNSTABLE"
FRAGILE = "FRAGILE"
DRIFT = "DRIFT"
VALIDATED = "VALIDATED"

# Lifecycle, distinct from the evidence status above. A strategy's LIFECYCLE
# says what it is allowed to do; its STATUS says what the evidence supports.
RESEARCH, CANDIDATE, VALIDATING, VALIDATED_LC, PRODUCTION = (
    "RESEARCH", "CANDIDATE", "VALIDATING", "VALIDATED", "PRODUCTION")
MONITORING, RETIRED = "MONITORING", "RETIRED"

TRADABLE_STATUSES = frozenset({VALIDATED})


@dataclass
class Verdict:
    strategy_id: str
    wallet: str
    label: str
    family: str
    status: str
    reasons: list = field(default_factory=list)
    is_sample: dict = field(default_factory=dict)
    oos: dict = field(default_factory=dict)
    alpha: dict = field(default_factory=dict)
    walkforward: dict = field(default_factory=dict)
    robustness: dict = field(default_factory=dict)
    p_value: float = 1.0
    score: float = 0.0
    describe: str = ""

    @property
    def tradable(self) -> bool:
        return self.status in TRADABLE_STATUSES

    def to_dict(self) -> dict:
        return {
            "strategy_id": self.strategy_id, "wallet": self.wallet,
            "label": self.label, "family": self.family, "status": self.status,
            "reasons": list(self.reasons), "p_value": round(self.p_value, 6),
            "score": round(self.score, 4), "describe": self.describe,
            "is_sample": self.is_sample, "oos": self.oos, "alpha": self.alpha,
            "walkforward": self.walkforward, "robustness": self.robustness,
        }


def walk_forward(strategy: CopyStrategy, observations: list, st: Settings,
                 tape: PriceTape, folds: int = 5) -> dict:
    """Split OOS observations into contiguous time folds and score each.

    Contiguous, never random: a random fold over a tape where one market
    appears many times puts the same market on both sides of the split, and
    the result stops being out-of-sample without anyone noticing.
    """
    if not observations:
        return {"folds": 0, "positive": 0, "fraction_positive": 0.0,
                "expectancies": []}
    obs = sorted(observations, key=lambda o: o.trade.ts)
    size = max(1, len(obs) // folds)
    exps, ns = [], []
    for i in range(folds):
        chunk = obs[i * size:(i + 1) * size] if i < folds - 1 else obs[i * size:]
        if not chunk:
            continue
        r = backtest.run(strategy, chunk, st, tape, collect_fills=False)
        if r.n_filled >= 3:
            exps.append(r.expectancy)
            ns.append(r.n_filled)
    if not exps:
        return {"folds": 0, "positive": 0, "fraction_positive": 0.0,
                "expectancies": []}
    pos = sum(1 for e in exps if e > 0)
    return {"folds": len(exps), "positive": pos,
            "fraction_positive": pos / len(exps),
            "expectancies": [round(e, 5) for e in exps], "fills": ns}


def perturbation(strategy: CopyStrategy, observations: list, st: Settings,
                 tape: PriceTape) -> dict:
    """Does the result survive nudging the parameters that produced it?

    A genuine edge is a region, not a point. If moving a price band by 0.05
    or the delay by one step destroys the result, what was found is the
    boundary of a noise pocket.
    """
    variants = []
    if strategy.min_price is not None:
        variants.append(replace(strategy,
                                min_price=max(0.02, strategy.min_price - 0.05)))
        variants.append(replace(strategy,
                                min_price=min(0.95, strategy.min_price + 0.05)))
    if strategy.max_price is not None:
        variants.append(replace(strategy,
                                max_price=min(0.98, strategy.max_price + 0.05)))
    if strategy.min_rel_notional:
        variants.append(replace(strategy,
                                min_rel_notional=strategy.min_rel_notional * 0.8))
    for d in (max(0, strategy.delay_secs // 2), strategy.delay_secs * 2 or 60):
        if d != strategy.delay_secs:
            variants.append(replace(strategy, delay_secs=d))
    if not variants:
        return {"n": 0, "positive": 0, "fraction_positive": 1.0,
                "note": "no perturbable parameters (naive baseline)"}
    exps = []
    for v in variants:
        r = backtest.run(v, observations, st, tape, collect_fills=False)
        if r.n_filled >= 5:
            exps.append(r.expectancy)
    if not exps:
        return {"n": 0, "positive": 0, "fraction_positive": 0.0,
                "note": "no perturbation produced enough fills"}
    pos = sum(1 for e in exps if e > 0)
    return {"n": len(exps), "positive": pos, "fraction_positive": pos / len(exps),
            "expectancies": [round(e, 5) for e in exps]}


def evaluate(strategy: CopyStrategy, is_obs: list, oos_obs: list,
             st: Settings, tape: PriceTape, book: BaselineBook,
             *, bh: stats.BHResult | None = None,
             universe_returns: list | None = None,
             deep: bool = True) -> Verdict:
    """Run one candidate through the full ladder.

    `is_obs` discovered it; `oos_obs` judges it. Nothing about the OOS window
    may influence which candidate reaches here -- that is the caller's
    responsibility and `discover.py` honours it by generating the whole grid
    blind.
    """
    cfg = st.strategy_b
    v = Verdict(strategy_id=strategy.strategy_id, wallet=strategy.wallet,
                label=strategy.label, family=strategy.family,
                status=INSUFFICIENT_EVIDENCE, describe=strategy.describe())

    is_res = backtest.run(strategy, is_obs, st, tape, collect_fills=False)
    v.is_sample = is_res.summary()

    oos_res = backtest.run(strategy, oos_obs, st, tape)
    v.oos = oos_res.summary()

    # 1. evidence
    if oos_res.n_filled < cfg.min_oos_fills:
        v.reasons.append(f"{oos_res.n_filled} out-of-sample fills, "
                         f"{cfg.min_oos_fills} needed")
        return v
    if len(oos_res.markets) < cfg.min_oos_markets:
        v.status = INSUFFICIENT_EVIDENCE
        v.reasons.append(f"{len(oos_res.markets)} out-of-sample markets, "
                         f"{cfg.min_oos_markets} needed - too few to "
                         "distinguish a strategy from a market")
        return v
    if oos_res.fill_rate < 0.25:
        v.status = UNPRICEABLE
        v.reasons.append(f"only {oos_res.fill_rate:.0%} of admitted signals "
                         "could be priced on the tape")
        return v

    # 2. does it make money out of sample, after costs
    if oos_res.expectancy <= 0:
        v.status = FAILED
        v.reasons.append(f"out-of-sample expectancy {oos_res.expectancy:+.4f}")
        return v

    # 3. is it distinguishable from the search that found it
    t = oos_res.t_stat()
    v.p_value = stats.two_sided_p(t)
    if bh is not None and not bh.significant(v.p_value):
        v.status = NOT_SIGNIFICANT
        v.reasons.append(
            f"p={v.p_value:.4f} does not clear the pass's BH threshold "
            f"{bh.threshold:.4f} over {bh.n_tested:,} hypotheses")
        return v

    # 4. is the edge the WALLET, or is it the market
    v.alpha = book.alpha_for(oos_res.fills, strategy.wallet)
    if v.alpha["matched"] > 0 and v.alpha["alpha"] <= 0:
        v.status = NO_WALLET_ALPHA
        v.reasons.append(
            f"strategy earns {v.alpha['strategy_edge']:+.4f} where the rest of "
            f"the market earned {v.alpha['population_edge']:+.4f} in the same "
            f"price band and week: wallet alpha {v.alpha['alpha']:+.4f}. This "
            "is the favourite-longshot bias, not the wallet.")
        return v
    if not v.alpha.get("controlled", False):
        v.reasons.append(
            f"NOTE: only {v.alpha.get('coverage', 0):.0%} of fills had a usable "
            "population comparison; alpha is weakly controlled")

    # 5. is it one market wearing a costume
    if oos_res.concentration() > cfg.max_concentration:
        v.status = CONCENTRATED
        v.reasons.append(
            f"{oos_res.concentration():.0%} of gross profit comes from one "
            f"market (limit {cfg.max_concentration:.0%})")
        return v

    if not deep:
        v.status = VALIDATED
        v.score = _score(v, oos_res)
        return v

    # 6. is it stable through time
    v.walkforward = walk_forward(strategy, oos_obs, st, tape,
                                 st.walkforward_folds)
    if v.walkforward["folds"] >= 3 and \
            v.walkforward["fraction_positive"] < cfg.min_walkforward_positive:
        v.status = UNSTABLE
        v.reasons.append(
            f"positive in {v.walkforward['positive']}/"
            f"{v.walkforward['folds']} walk-forward folds")
        return v

    # 7. is it a region or a point
    lo, hi = stats.block_bootstrap_ci(oos_res.returns, seed=st.seed)
    pert = perturbation(strategy, oos_obs, st, tape)
    v.robustness = {"bootstrap_lo": round(lo, 5), "bootstrap_hi": round(hi, 5),
                    "perturbation": pert}
    if lo <= 0:
        v.status = FRAGILE
        v.reasons.append(
            f"block-bootstrap 95% CI [{lo:+.4f}, {hi:+.4f}] includes zero once "
            "the correlation between trades in the same market is respected")
        return v
    if pert["n"] >= 2 and pert["fraction_positive"] < 0.5:
        v.status = FRAGILE
        v.reasons.append(
            f"only {pert['positive']}/{pert['n']} parameter perturbations stay "
            "positive - this is the edge of a noise pocket, not a region")
        return v

    # 8. does it beat random entries in the same pool
    if universe_returns:
        p = stats.placebo_p(oos_res.returns, universe_returns, seed=st.seed)
        v.robustness["placebo_p"] = round(p, 4)
        if p > 0.10:
            v.status = DRIFT
            v.reasons.append(
                f"random entries of the same count in the same pool matched or "
                f"beat this {p:.0%} of the time - the result is drift, not a "
                "signal")
            return v

    v.status = VALIDATED
    v.score = _score(v, oos_res)
    return v


def _score(v: Verdict, res: backtest.Result) -> float:
    """The multi-factor leaderboard score.

    Deliberately NOT expectancy. A tiny-sample extraordinary return must not
    dominate the ranking, so sample size enters as a saturating term and
    concentration, drawdown and instability all subtract. Wallet alpha is
    weighted heavily because it is the only term that cannot be earned by
    accident.
    """
    import math
    a = res.asymmetry()
    n_term = min(1.0, math.log1p(res.n_filled) / math.log1p(300))
    mkt_term = min(1.0, len(res.markets) / 30.0)
    alpha = v.alpha.get("alpha", 0.0)
    stability = v.walkforward.get("fraction_positive", 0.5)
    pf = min(a["profit_factor"], 5.0) / 5.0
    conc_penalty = max(0.0, res.concentration() - 0.3)
    return round(
        (0.30 * min(res.expectancy, 0.5) / 0.5
         + 0.25 * max(0.0, min(alpha, 0.3)) / 0.3
         + 0.15 * pf
         + 0.15 * stability
         + 0.10 * n_term
         + 0.05 * mkt_term
         - 0.20 * conc_penalty), 4)


def assign_status(verdict: Verdict) -> str:
    """The single authority. Every other module reads `verdict.status`."""
    return verdict.status
