"""
Layer 1+2: turning wallet and trade evidence into a **calibrated probability**.

This exists because of a specific, valid criticism of the previous engine: it
blended a wallet score and a market-quality score into a 0-1 number, compared
that number against a threshold, and bought. That number was never a
probability of anything, so a "score" of 0.93 could trigger a buy at an ask of
0.80 — an inference with no meaning behind it.

The model here is deliberately conservative, and its shape matters more than
its coefficients:

**The market price is the starting point, not our opinion.** On a liquid
prediction market the ask already encodes the crowd's probability. Any honest
estimate begins there and asks a narrower question: *does our evidence justify
moving it, and by how much?* Starting from a wallet score instead is what
allowed the old engine to disagree with the market by 13 points on no evidence
at all.

Evidence moves the price in **log-odds**, which is the only space where
"this evidence is worth +0.3" means the same thing at 0.10 as at 0.90. In
probability space a fixed nudge is enormous near the tails and negligible in
the middle.

Every piece of evidence is scaled by how much of it there is. A wallet with
eleven trades cannot move the price as far as one with two hundred, and that
is enforced here rather than left to the caller.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from ..models import BridgeContext, MarketFeatures, OutcomeQuote, WalletIntel

# How far, in log-odds, the strongest possible evidence may move the market's
# own price. 1.0 is about 0.50 -> 0.73, or 0.80 -> 0.92: a large move, and a
# deliberate ceiling. Anything that lets evidence override the market entirely
# is asserting we know better than every other participant combined.
MAX_SHIFT = 1.0

# Below this many scored trades a wallet contributes nothing at all. It is not
# that its record is bad — it is that we cannot tell, and a guess dressed up as
# a probability is worse than no opinion.
MIN_WALLET_SAMPLE = 20


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x <= -30:
        return 0.0
    if x >= 30:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


@dataclass
class Evidence:
    """One reason to disagree with the market, and how strongly."""

    name: str
    shift: float                  # in log-odds; positive favours the outcome
    detail: dict = field(default_factory=dict)


@dataclass
class ProbabilityEstimate:
    """What we think the true probability is, and how we got there."""

    market_price: float           # the crowd's view
    probability: float            # ours
    evidence: list[Evidence] = field(default_factory=list)
    confidence: float = 0.0       # 0-1: how much evidence stands behind it

    @property
    def edge(self) -> float:
        """How far we disagree with the market, in probability points."""
        return self.probability - self.market_price

    def to_dict(self) -> dict:
        return {
            "marketPrice": round(self.market_price, 4),
            "probability": round(self.probability, 4),
            "edge": round(self.edge, 4),
            "confidence": round(self.confidence, 4),
            "evidence": [{"name": e.name, "shift": round(e.shift, 4), **e.detail}
                         for e in self.evidence],
        }


def _sample_weight(sample: int) -> float:
    """0-1, saturating. 20 trades barely counts; 200 counts fully.

    This is the direct answer to "100% over 11 trades is not as informative as
    a strong result over hundreds". It is applied to the *evidence*, not just
    to the wallet's own score, so a thin record cannot move a price far however
    good it looks.
    """
    if sample < MIN_WALLET_SAMPLE:
        return 0.0
    return min(1.0, math.log1p(sample - MIN_WALLET_SAMPLE) / math.log1p(180))


def estimate(quote: OutcomeQuote, market: MarketFeatures,
             context: BridgeContext,
             wallet_evidence: Optional[list[tuple[WalletIntel, float]]] = None,
             ) -> ProbabilityEstimate:
    """Estimate P(this outcome settles at $1.00).

    ``wallet_evidence`` is ``(wallet, signed_usdc)`` for wallets that acted on
    this outcome recently — positive for buying it, negative for selling.
    """
    # The mid is a better read of the crowd's view than the ask, which includes
    # the spread we would have to pay. Costs are charged later, in the EV
    # calculation, so including them here would penalise us twice.
    price = quote.mid or quote.ask or quote.last or 0.5
    price = min(max(price, 0.01), 0.99)

    base = _logit(price)
    evidence: list[Evidence] = []
    total_weight = 0.0

    for wallet, signed_usdc in (wallet_evidence or []):
        weight = _sample_weight(wallet.sample)
        if weight <= 0:
            continue
        # A hedger's leg says nothing about direction — it is one half of a
        # pair. Its evidence is suppressed in proportion to how much it hedges.
        weight *= max(0.0, 1.0 - wallet.hedge_rate)
        if weight <= 0:
            continue

        # Centre the wallet's skill on the population's own average, so
        # "average wallet bought this" contributes nothing rather than
        # contributing a positive simply for existing.
        skill = (wallet.score - 0.5) * 2.0          # -1 .. +1
        direction = 1.0 if signed_usdc >= 0 else -1.0
        # Conviction from size, saturating: a $10,000 buy is stronger evidence
        # than $100, but not a hundred times stronger.
        size = min(1.0, math.log1p(abs(signed_usdc)) / math.log1p(5_000.0))

        shift = skill * direction * size * weight * 0.6
        if abs(shift) < 1e-6:
            continue
        evidence.append(Evidence(
            name=f"wallet:{wallet.label}", shift=shift,
            detail={"score": round(wallet.score, 3), "sample": wallet.sample,
                    "usdc": round(signed_usdc, 2),
                    "hedgeRate": round(wallet.hedge_rate, 3),
                    "sampleWeight": round(weight, 3)}))
        total_weight += weight

    # Market-level flow: unusual capital arriving is weak, undirected evidence,
    # so it is capped well below what a proven wallet may contribute.
    intel = context.market_intel.get(market.market_id)
    if intel is not None and intel.flow_z > 2.0 and abs(intel.net_usdc) > 100:
        direction = 1.0 if intel.net_usdc > 0 else -1.0
        shift = direction * min(0.25, intel.flow_z / 40.0)
        evidence.append(Evidence(
            name="market flow", shift=shift,
            detail={"flowZ": round(intel.flow_z, 2),
                    "netUsdc": round(intel.net_usdc, 2)}))
        total_weight += 0.25

    total_shift = max(-MAX_SHIFT, min(MAX_SHIFT, sum(e.shift for e in evidence)))
    probability = _sigmoid(base + total_shift)

    return ProbabilityEstimate(
        market_price=price, probability=probability, evidence=evidence,
        confidence=min(1.0, total_weight))
