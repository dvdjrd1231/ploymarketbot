"""Multiple independent probability estimates, and honest uncertainty about them.

A single model produces a number. An ensemble produces a number *and* a
measurement of how much the estimators disagree, which is the part that
determines whether the number should be acted on.

Estimators here are deliberately heterogeneous — a market-implied read, a
base-rate read, a Bayesian posterior, a wallet-conditioned read, a
news-conditioned read, a microstructure read, a time-series read and a
cross-market read. Averaging eight variations of the same idea would produce a
tight confidence interval around a shared error, which is the most dangerous
output a probability engine can emit.

Two properties worth stating:

  * **The market price is an estimator, not the truth.** It enters the ensemble
    weighted like anything else. But it is also the thing we are trying to
    beat, so `edge` is always measured against it and never against the
    ensemble mean.

  * **Disagreement widens the interval; it does not cancel out.** With
    estimators split 0.30/0.70 the mean is 0.50, which looks like a confident
    coin flip. The interval reported here is driven by dispersion, so that case
    surfaces as 0.50 ± 0.20 and fails the signal gate rather than passing it.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from ..core.canon import EvidenceState


@dataclass
class Estimate:
    name: str
    probability: float
    weight: float                    # 0..1 prior trust in this estimator
    basis: str = ""
    n: int = 0                       # observations behind it, where meaningful

    def to_dict(self) -> dict:
        return {"name": self.name, "probability": round(self.probability, 5),
                "weight": round(self.weight, 4), "basis": self.basis, "n": self.n}


@dataclass
class Ensemble:
    estimates: list = field(default_factory=list)
    market_probability: float = 0.0

    @property
    def n(self) -> int:
        return len(self.estimates)

    @property
    def calibrated_probability(self) -> float:
        """Weighted mean, shrunk toward the market price.

        Shrinkage is not timidity: with a handful of estimators over 90 days of
        one venue's data, the ensemble's own error is comparable to its claimed
        edge. Shrinking toward the market makes the system require *more*
        agreement to claim a large edge, which is the correct asymmetry when
        being wrong costs real money and being right merely costs an
        opportunity.
        """
        if not self.estimates:
            return self.market_probability
        w = sum(e.weight for e in self.estimates) or 1e-9
        raw = sum(e.probability * e.weight for e in self.estimates) / w
        # Shrinkage falls as effective sample size rises.
        eff = sum(e.weight for e in self.estimates)
        lam = 1.0 / (1.0 + eff)          # 0.5 with one unit-weight estimator
        return round(raw * (1 - lam) + self.market_probability * lam, 6)

    @property
    def dispersion(self) -> float:
        ps = [e.probability for e in self.estimates]
        return round(statistics.pstdev(ps), 6) if len(ps) > 1 else 0.0

    @property
    def disagreement(self) -> float:
        """0..1. A 0.25 standard deviation is treated as total disagreement."""
        return round(min(1.0, self.dispersion / 0.25), 4)

    @property
    def confidence_interval(self) -> tuple[float, float]:
        """Dispersion-driven, with a floor that never claims false precision.

        The floor matters: three estimators that happen to agree exactly would
        otherwise report a zero-width interval, which would be a statement
        about luck rather than about knowledge.
        """
        p = self.calibrated_probability
        eff = max(1.0, sum(e.weight for e in self.estimates))
        # Standard error of the weighted mean, floored by binomial noise at the
        # smallest sample any estimator was built from.
        n_min = min((e.n for e in self.estimates if e.n > 0), default=30)
        floor = math.sqrt(max(p * (1 - p), 0.01) / max(n_min, 1))
        se = max(self.dispersion / math.sqrt(eff), floor, 0.01)
        return (round(max(0.0, p - 1.96 * se), 5),
                round(min(1.0, p + 1.96 * se), 5))

    @property
    def evidence_strength(self) -> float:
        """0..1: how much independent evidence is behind the estimate."""
        if not self.estimates:
            return 0.0
        eff = sum(e.weight for e in self.estimates)
        return round(min(1.0, eff / 3.0) * (1.0 - self.disagreement), 4)

    @property
    def edge(self) -> float:
        return round(self.calibrated_probability - self.market_probability, 6)

    @property
    def expected_value_per_dollar(self) -> float:
        """EV of $1 spent buying at the market price, at our probability."""
        p = self.calibrated_probability
        px = self.market_probability
        if not (0 < px < 1):
            return 0.0
        return round(p * (1 - px) / px - (1 - p), 6)

    def to_dict(self) -> dict:
        lo, hi = self.confidence_interval
        return {"calibrated_probability": self.calibrated_probability,
                "market_probability": round(self.market_probability, 5),
                "edge": self.edge,
                "confidence_interval": [lo, hi],
                "dispersion": self.dispersion,
                "model_disagreement": self.disagreement,
                "evidence_strength": self.evidence_strength,
                "expected_value_per_dollar": self.expected_value_per_dollar,
                "n_estimators": self.n,
                "estimates": [e.to_dict() for e in self.estimates]}


def build(ev: EvidenceState, ctx: dict, verdicts: list | None = None) -> Ensemble:
    """Assemble every estimate that can honestly be produced from this state.

    An estimator that cannot be computed is OMITTED, never defaulted to 0.5.
    A 0.5 placeholder is an active claim that the outcome is a coin flip, and
    enough of them drag every estimate toward the middle while making the
    dispersion look reassuringly small.
    """
    mkt = float(ctx.get("market_probability") or ev.price.get("last") or 0.0)
    ens = Ensemble(market_probability=mkt)
    add = ens.estimates.append

    # 1 — market implied
    if 0 < mkt < 1:
        add(Estimate("market_implied", mkt, 1.0,
                     "last printed price", ev.price.rows))

    # 2 — historical base rate in this price band
    base = ctx.get("band_baseline") or {}
    if int(base.get("n") or 0) >= 200:
        add(Estimate("base_rate", float(base["hit_rate"]),
                     min(1.0, base["n"] / 5000.0),
                     f"outcome rate in band {base.get('band')}",
                     int(base["n"])))

    # 3 — Bayesian posterior, taken from Agent 7 rather than recomputed
    for v in (verdicts or []):
        if v.agent == "BAYESIAN_PROBABILITY" and v.probability is not None:
            add(Estimate("bayesian", float(v.probability), 0.8,
                         "Beta-Binomial over the band prior"))
            break

    # 4 — wallet-conditioned
    dna = ctx.get("wallet_dna") or {}
    tops = ev.wallets.get("top") or [] if ev.wallets.ok else []
    profiled = [t for t in tops if t["wallet"] in dna]
    if profiled:
        alphas = [float(dna[t["wallet"]].get("alpha_vs_band") or 0.0)
                  for t in profiled]
        tilt = statistics.fmean(alphas)
        add(Estimate("wallet_conditioned", _clip(mkt + tilt),
                     min(0.7, 0.12 * len(profiled)),
                     f"market price tilted by mean alpha {tilt:+.4f} of "
                     f"{len(profiled)} profiled wallets", len(profiled)))

    # 5 — news-conditioned
    if ev.news.ok and int(ev.news.get("relevant") or 0) > 0:
        d = float(ev.news.get("weighted_direction") or 0.0)
        m = float(ev.news.get("max_magnitude") or 0.0)
        if abs(d) > 0.01:
            add(Estimate("news_conditioned", _clip(mkt + d * m * 0.2),
                         min(0.6, 0.2 * int(ev.news.get("relevant"))),
                         f"direction {d:+.3f} x magnitude {m:.3f}",
                         int(ev.news.get("relevant"))))

    # 6 — microstructure drift
    if ev.price.ok:
        vel = float(ev.price.get("velocity_1h") or 0.0)
        if abs(vel) > 0.005:
            # One hour of drift, damped hard: extrapolating tape velocity is
            # the single easiest way to build a momentum system that buys tops.
            add(Estimate("microstructure", _clip(mkt + vel * 0.25), 0.35,
                         f"1h velocity {vel:+.4f}, damped 4x", ev.price.rows))

    # 7 — time-series, from Agent 8
    for v in (verdicts or []):
        if v.agent == "TIME_SERIES" and v.stance.value != "ABSTAIN":
            sign = 1.0 if v.stance.value == "FOR" else -1.0
            add(Estimate("time_series", _clip(mkt + sign * 0.02 * v.confidence),
                         0.3 * v.confidence, v.thesis[:80]))
            break

    # 8 — cross-market consistency
    sib = ctx.get("sibling_prices") or {}
    if sib:
        total = mkt + sum(sib.values())
        if abs(total - 1.0) > 0.02:
            # Normalise this outcome's share of the total, which is the
            # arbitrage-free reading if the outcomes really are exclusive.
            add(Estimate("cross_market", _clip(mkt / total) if total > 0 else mkt,
                         0.4, f"outcomes sum to {total:.4f}; normalised",
                         len(sib) + 1))

    return ens


def _clip(p: float) -> float:
    return max(0.001, min(0.999, p))
