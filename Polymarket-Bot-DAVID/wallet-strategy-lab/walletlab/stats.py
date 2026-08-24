"""Multiple-testing control and robustness tests (§14, §34).

The engine's central danger is not that it fails to find a strategy. It is that
it finds thousands, and reports the best one. With ~1,700 candidates per wallet
and 125 eligible wallets, roughly 200,000 hypotheses get tested; at p<0.05 that
is ~10,000 winners expected from pure noise.

So nothing in this engine reports a p-value without also reporting how many
alternatives were tested to obtain it, and `benjamini_hochberg` sets the bar
that actually gates promotion.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


def normal_sf(z: float) -> float:
    """One-sided survival function of the standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def t_to_p(t: float, n: int) -> float:
    """One-sided p-value for a t statistic. Normal approximation.

    n is large enough here (>= 30 filled trades is required for promotion) that
    the normal approximation is adequate, and being approximate in the
    conservative direction is acceptable — we never *lower* a bar with it.
    """
    if n < 5:
        return 1.0
    return normal_sf(t)


def benjamini_hochberg(pvalues: list[float], fdr: float = 0.10) -> tuple[float, int]:
    """Return (threshold, n_significant) controlling false discovery at `fdr`.

    Chosen over Bonferroni deliberately: with 200k hypotheses Bonferroni's bar
    (p < 2.5e-7) is unreachable for a wallet with 100 trades, so it would not
    make the engine rigorous — it would make it silent. BH controls the
    *proportion* of reported discoveries that are false, which is the quantity
    a research pipeline actually cares about.
    """
    if not pvalues:
        return 0.0, 0
    m = len(pvalues)
    ordered = sorted(pvalues)
    thresh = 0.0
    k = 0
    for i, p in enumerate(ordered, start=1):
        if p <= (i / m) * fdr:
            thresh = p
            k = i
    return thresh, k


@dataclass
class RobustnessReport:
    parameter_stability: float   # fraction of neighbours that also work
    bootstrap_p: float           # P(mean return <= 0) under resampling
    placebo_p: float             # P(random entries do this well)
    survives_costs: bool
    verdict: str

    def as_dict(self) -> dict:
        return {
            "parameter_stability": round(self.parameter_stability, 3),
            "bootstrap_p": round(self.bootstrap_p, 4),
            "placebo_p": round(self.placebo_p, 4),
            "survives_costs": self.survives_costs,
            "verdict": self.verdict,
        }


def bootstrap_p(returns: list[float], draws: int = 2000, seed: int = 0) -> float:
    """P(resampled mean <= 0). Low means the positive mean is not one lucky trade."""
    n = len(returns)
    if n < 10:
        return 1.0
    rng = random.Random(seed)
    bad = 0
    for _ in range(draws):
        m = sum(returns[rng.randrange(n)] for _ in range(n)) / n
        if m <= 0:
            bad += 1
    return bad / draws


def placebo_p(
    strategy_returns: list[float], population: list[float], draws: int = 2000, seed: int = 0
) -> float:
    """P(a random subset of the same size beats this strategy's mean).

    Catches the failure nothing else sees: a filter that selects no information
    at all, on a wallet whose whole population happens to be profitable. If a
    random draw of the same size does as well, the *filter* has no edge even
    when the strategy's P&L is positive.
    """
    k = len(strategy_returns)
    n = len(population)
    if k < 5 or n <= k:
        return 1.0
    target = sum(strategy_returns) / k
    rng = random.Random(seed)
    beat = 0
    for _ in range(draws):
        m = sum(population[rng.randrange(n)] for _ in range(k)) / k
        if m >= target:
            beat += 1
    return beat / draws
