"""Behaviour matching: does THIS signal look like the reference behaviour?

This is what makes Strategy B a strategy rather than a subscription. The
engine does not ask "did RN1 trade?" -- it asks "is this action of the kind RN1
takes when RN1 is being profitable?", which is a question any wallet's action
can be scored against, including wallets nobody has studied.

The score is a weighted agreement across independent behavioural dimensions,
each normalised into [0,1]. Deliberately transparent arithmetic rather than a
fitted model: a match score that cannot be explained to the operator will be
overridden by the operator, and a match score fitted on outcomes would be
look-ahead wearing a friendly name.

Weights are not tuned on returns. They encode which dimensions were measured to
carry information (see research/features.py), and any change to them is a
change to the strategy, so it goes through the ladder like anything else.
"""

from __future__ import annotations

from dataclasses import dataclass

from .decompose import WalletProfile


def _in_range(x: float, lo: float, hi: float, softness: float = 0.15) -> float:
    """1.0 inside [lo, hi], decaying to 0 outside over `softness` of the width.

    Soft edges matter: a hard band makes the score a step function, which turns
    the match threshold into an arbitrary cliff and makes every downstream
    diagnostic discontinuous.
    """
    if hi < lo:
        lo, hi = hi, lo
    width = max(hi - lo, 1e-9)
    pad = width * softness + 1e-9
    if lo <= x <= hi:
        return 1.0
    d = (lo - x) if x < lo else (x - hi)
    return max(0.0, 1.0 - d / pad)


def _ratio_score(x: float, target: float, tol: float = 0.5) -> float:
    if target <= 0:
        return 0.5              # no opinion rather than a free point
    r = x / target
    lo, hi = 1.0 - tol, 1.0 + tol
    if lo <= r <= hi:
        return 1.0
    return max(0.0, 1.0 - (abs(r - 1.0) - tol) / max(tol, 1e-9))


@dataclass
class MatchResult:
    score: float
    components: dict
    reason: str = ""

    def __bool__(self) -> bool:
        return self.score > 0


# Dimension weights. They sum to 1.0; `test_behavior.py` asserts it, because a
# silent drift in the sum turns the threshold into a different threshold.
WEIGHTS = {
    "price": 0.30,          # where in probability space the wallet operates
    "conviction": 0.20,     # is this a normal bet or a big one, for them
    "horizon": 0.15,        # how far from settlement they act
    "tape": 0.10,           # do they trade thin or busy books
    "chase": 0.10,          # do they enter into a move or ahead of it
    "opening": 0.10,        # opening position vs adding
    "discipline": 0.05,     # are they inside their own loss tolerance
}


class BehaviorMatcher:
    """Scores an observation against a reconstructed profile."""

    def __init__(self, profile: WalletProfile) -> None:
        self.profile = profile
        e = profile.entry
        self._price_lo = e.price.get("p10", 0.05)
        self._price_hi = e.price.get("p90", 0.95)
        self._rel_target = max(e.rel_notional.get("p50", 1.0), 0.1)
        self._horizon_lo = e.secs_to_settle.get("p10", 0.0)
        self._horizon_hi = e.secs_to_settle.get("p90", 0.0)
        self._prints_p25 = e.market_prints.get("p25", 0.0)
        self._move_p90 = abs(e.market_move.get("p90", 0.0)) or 0.05
        self._opening = e.opening_entry_share
        self._loss_tolerance = max(1, profile.risk.max_consec_losses)

    def score(self, o) -> MatchResult:
        c: dict = {}

        c["price"] = _in_range(o.price, self._price_lo, self._price_hi, 0.30)
        c["conviction"] = _ratio_score(o.rel_notional, self._rel_target, 0.75)

        if self._horizon_hi > 0 and o.secs_to_settle > 0:
            c["horizon"] = _in_range(o.secs_to_settle, self._horizon_lo,
                                     self._horizon_hi, 0.50)
        else:
            # No settlement clock for this trade. The honest score is "no
            # opinion" -- 0.5 -- not 1.0, which would hand out free agreement
            # on precisely the trades we know least about.
            c["horizon"] = 0.5

        c["tape"] = 1.0 if o.market_recent_prints >= self._prints_p25 else (
            o.market_recent_prints / max(self._prints_p25, 1.0))

        # Chase: is the pre-entry move within what this wallet normally
        # tolerates? A wallet that never enters after a 10-point run should not
        # match a signal that does.
        c["chase"] = max(0.0, 1.0 - max(0.0, abs(o.market_price_move)
                                        - self._move_p90) / max(self._move_p90, 1e-6))

        opening = not o.w_token_repeat
        c["opening"] = (self._opening if not opening else 1.0) if self._opening > 0.5 \
            else (1.0 - self._opening if opening else 1.0)
        c["opening"] = min(1.0, max(0.0, c["opening"]))

        c["discipline"] = 1.0 if o.w_consec_losses <= self._loss_tolerance else \
            max(0.0, 1.0 - (o.w_consec_losses - self._loss_tolerance) / 3.0)

        total = sum(WEIGHTS[k] * v for k, v in c.items())
        weakest = min(c, key=c.get)
        return MatchResult(
            score=round(total, 4),
            components={k: round(v, 3) for k, v in c.items()},
            reason=f"weakest dimension: {weakest} at {c[weakest]:.2f}")

    def matches(self, o, threshold: float) -> tuple[bool, MatchResult]:
        r = self.score(o)
        return r.score >= threshold, r


class CompositeMatcher:
    """Match against a FAMILY -- several wallets that behave alike.

    Preferred over a single-wallet matcher wherever a family exists: a profile
    built from four independent wallets is far harder to overfit than one built
    from the wallet whose returns suggested the idea in the first place.
    """

    def __init__(self, profiles: list) -> None:
        if not profiles:
            raise ValueError("a family needs at least one profile")
        self.matchers = [BehaviorMatcher(p) for p in profiles]
        self.wallets = [p.wallet for p in profiles]

    def score(self, o) -> MatchResult:
        results = [m.score(o) for m in self.matchers]
        # The MEDIAN, not the max: taking the best of N matchers would make a
        # family score higher the more members it has, which rewards adding
        # wallets rather than agreement between them.
        scores = sorted(r.score for r in results)
        mid = scores[len(scores) // 2] if len(scores) % 2 else \
            (scores[len(scores) // 2 - 1] + scores[len(scores) // 2]) / 2
        best = max(results, key=lambda r: r.score)
        return MatchResult(score=round(mid, 4), components=best.components,
                           reason=f"median of {len(results)} family members")
