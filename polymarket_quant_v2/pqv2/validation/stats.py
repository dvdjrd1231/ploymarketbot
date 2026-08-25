"""Multiple testing, bootstrap, and the significance machinery.

The reason this module exists: a sweep tests ~5,184 transformations per wallet.
Over 60 wallets that is ~311,000 hypotheses. At p<0.05, roughly 15,500 of them
are expected to "win" by chance alone. Any engine that reports the winners
without reporting the denominator is a false-discovery generator wearing a lab
coat -- and it would look like the best result the project has ever produced.

So promotion is gated on a Benjamini-Hochberg threshold computed over the WHOLE
pass. A p-value can never be quoted here without the search that produced it.

Standard library only: no scipy. The normal CDF is Abramowitz-Stegun via erf,
which is exact to ~1e-15 and available in `math`.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def two_sided_p(t: float) -> float:
    """p-value for a t-like statistic, using the normal approximation.

    Honest about itself: with n >= 30 (the minimum this engine accepts for any
    promotion) the normal approximation to the t distribution is accurate to
    better than the third decimal, which is far finer than the effect sizes
    being judged.
    """
    return 2.0 * (1.0 - normal_cdf(abs(t)))


def mean_std(xs) -> tuple[float, float]:
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else 0.0), 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, math.sqrt(max(var, 0.0))


def t_stat(xs) -> float:
    n = len(xs)
    if n < 5:
        return 0.0
    m, sd = mean_std(xs)
    return m / (sd / math.sqrt(n)) if sd > 0 else 0.0


@dataclass
class BHResult:
    threshold: float
    n_tested: int
    n_significant: int

    def significant(self, p: float) -> bool:
        return p <= self.threshold


def benjamini_hochberg(pvalues, fdr: float = 0.10) -> BHResult:
    """The threshold below which a p-value survives the pass's own search.

    Note `fdr` is the FALSE DISCOVERY rate, not a per-test alpha: of the
    hypotheses declared significant, at most `fdr` of them are expected to be
    noise. That is the right control for a search, and it tightens
    automatically as the search grows -- which is exactly the property the
    brief's "multiple-testing illusions" rule needs.
    """
    ps = sorted(p for p in pvalues if p == p)     # drop NaN
    n = len(ps)
    if n == 0:
        return BHResult(0.0, 0, 0)
    threshold = 0.0
    k = 0
    for i, p in enumerate(ps, start=1):
        if p <= (i / n) * fdr:
            threshold = p
            k = i
    return BHResult(threshold, n, k)


def bootstrap_ci(returns, *, draws: int = 1000, alpha: float = 0.05,
                 seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap CI for mean per-trade return.

    Answers the question a t-statistic assumes away: prediction-market returns
    are violently non-normal (a 0.05 entry that resolves YES returns +1900%),
    so the parametric interval is not trustworthy on its own.
    """
    n = len(returns)
    if n < 10:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        s = 0.0
        for _ in range(n):
            s += returns[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo = means[int(draws * alpha / 2)]
    hi = means[min(draws - 1, int(draws * (1 - alpha / 2)))]
    return lo, hi


def block_bootstrap_ci(returns, *, block: int = 10, draws: int = 500,
                       alpha: float = 0.05, seed: int = 0) -> tuple[float, float]:
    """Bootstrap that preserves local dependence.

    Copy trades are not independent: one wallet loading one market produces a
    run of highly correlated returns. Resampling single trades treats that run
    as many independent wins and narrows the interval fraudulently. Resampling
    BLOCKS keeps the run intact.
    """
    n = len(returns)
    if n < block * 3:
        return bootstrap_ci(returns, draws=draws, alpha=alpha, seed=seed)
    rng = random.Random(seed)
    nblocks = n // block
    means = []
    for _ in range(draws):
        acc = []
        for _ in range(nblocks):
            start = rng.randrange(0, n - block)
            acc.extend(returns[start:start + block])
        means.append(sum(acc) / len(acc))
    means.sort()
    return means[int(draws * alpha / 2)], means[min(draws - 1,
                                                    int(draws * (1 - alpha / 2)))]


def placebo_p(returns, universe, *, draws: int = 400, seed: int = 0) -> float:
    """Does the strategy beat random entries of the same count drawn from the
    same pool?

    Catches the failure nothing else sees: a rule that captured broad market
    drift rather than a signal. Such a candidate passes leave-one-out, temporal
    split AND dispersion, because drift is broad, stable and replicated.
    """
    n = len(returns)
    if n < 10 or len(universe) < n * 2:
        return 1.0
    actual = sum(returns) / n
    rng = random.Random(seed)
    beat = 0
    for _ in range(draws):
        s = 0.0
        for _ in range(n):
            s += universe[rng.randrange(len(universe))]
        if s / n >= actual:
            beat += 1
    return (beat + 1) / (draws + 1)


def risk_of_ruin(expectancy: float, std: float, fraction: float,
                 ruin_level: float = 0.5, trials: int = 2000,
                 horizon: int = 1000, seed: int = 0) -> float:
    """Probability equity falls to `ruin_level` within `horizon` trades.

    Monte Carlo rather than the closed form, because the closed form assumes a
    two-outcome bet and these returns have a long right tail. Not a forecast --
    a comparison device between sizing choices.
    """
    if std <= 0 or fraction <= 0:
        return 0.0
    rng = random.Random(seed)
    ruined = 0
    for _ in range(trials):
        eq = 1.0
        for _ in range(horizon):
            r = rng.gauss(expectancy, std)
            eq *= (1.0 + fraction * r)
            if eq <= ruin_level:
                ruined += 1
                break
    return ruined / trials
