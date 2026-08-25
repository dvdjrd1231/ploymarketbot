"""Statistics for the research pass.

Everything here is stdlib. The pieces that matter:

**Benjamini-Hochberg over the whole pass.** Not per family, not per wallet —
the correction must be computed over every test the pass performed, including
the ones that were generated and discarded, or the reported significance is a
statement about a search that did not happen.

**Block bootstrap, not i.i.d.** Prediction-market returns are serially
dependent: one market resolves many positions at once, so adjacent rows share
an outcome. An i.i.d. bootstrap over dependent data produces a confidence
interval far too narrow, which is the quiet way a backtest overstates itself.

**A one-sided test.** We are asking "is this better than nothing", not "is this
different from nothing". Using a two-sided p-value here would be twice as
permissive as intended in the direction we care about.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field


def mean(xs) -> float:
    return statistics.fmean(xs) if xs else 0.0


def t_stat(returns: list) -> tuple:
    """(t, dof). Zero when the sample cannot support a test."""
    n = len(returns)
    if n < 3:
        return 0.0, 0
    m = statistics.fmean(returns)
    var = statistics.pvariance(returns) * n / (n - 1)
    if var <= 0:
        return 0.0, n - 1
    return m / math.sqrt(var / n), n - 1


def _t_sf(t: float, dof: int) -> float:
    """One-sided survival function of Student's t, no SciPy.

    Uses the regularised incomplete beta via a continued fraction. Accurate to
    well past the precision a p-value is quoted at here, and deterministic —
    an approximation whose error is unknown would undermine the whole point of
    quoting a threshold.
    """
    if dof <= 0:
        return 1.0
    if t <= 0:
        return 1.0 - _t_sf(-t, dof) if t < 0 else 0.5
    x = dof / (dof + t * t)
    return 0.5 * _betainc(dof / 2.0, 0.5, x)


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = (math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b))
    front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta) / a
    # Lentz's algorithm for the continued fraction.
    f, c, d = 1.0, 1.0, 0.0
    for i in range(0, 300):
        m = i // 2
        if i == 0:
            num = 1.0
        elif i % 2 == 0:
            num = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            num = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + num * d
        if abs(d) < 1e-30:
            d = 1e-30
        d = 1.0 / d
        c = 1.0 + num / c
        if abs(c) < 1e-30:
            c = 1e-30
        f *= c * d
        if abs(1.0 - c * d) < 1e-12:
            break
    return front * (f - 1.0)


# Below this, double precision cannot distinguish a p-value from zero. A
# reported p of exactly 0.0 is an artefact of arithmetic, not a measurement,
# and printing it invites a confidence the number cannot carry.
P_FLOOR = 1e-16


def p_value_one_sided(returns: list) -> float:
    """P(observing a mean this positive by chance | true mean is zero).

    Floored at `P_FLOOR`. The floor is not caution, it is accuracy: the
    continued fraction underflows well before the true tail probability does,
    so anything smaller is "too small to measure" rather than "zero".
    """
    t, dof = t_stat(returns)
    if dof <= 0:
        return 1.0
    return max(P_FLOOR, min(1.0, _t_sf(t, dof)))


def format_p(p: float) -> str:
    """Render a p-value without claiming precision the arithmetic lacks."""
    return f"<{P_FLOOR:g}" if p <= P_FLOOR else f"{p:.3g}"


def block_bootstrap_ci(returns: list, *, draws: int = 2000,
                       block: int = 0, seed: int = 20260825,
                       alpha: float = 0.05) -> tuple:
    """(lo, hi, share_positive) for the mean, by moving-block bootstrap.

    `share_positive` is the fraction of resamples with a positive mean — a
    more directly useful number than the interval when the question is "how
    confident are we this is not zero".
    """
    n = len(returns)
    if n < 10:
        return (0.0, 0.0, 0.0)
    # Rule-of-thumb block length n^(1/3), which balances preserving dependence
    # against having enough distinct blocks to resample.
    b = block or max(2, int(round(n ** (1 / 3))))
    from ..accel import default as accel_default
    means = accel_default().call("block_bootstrap", list(returns), draws, b, seed)
    if not means:
        return (0.0, 0.0, 0.0)
    means.sort()
    lo = means[int(alpha / 2 * (len(means) - 1))]
    hi = means[int((1 - alpha / 2) * (len(means) - 1))]
    pos = sum(1 for x in means if x > 0) / len(means)
    return (lo, hi, pos)


@dataclass
class BHResult:
    alpha: float
    n_tests: int
    threshold: float
    n_significant: int
    ranked: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"alpha": self.alpha, "n_tests": self.n_tests,
                "threshold": self.threshold,
                "n_significant": self.n_significant}


def benjamini_hochberg(p_values: list, alpha: float = 0.10,
                       n_tests: int | None = None) -> BHResult:
    """BH step-up over the WHOLE pass.

    `n_tests` defaults to len(p_values) but should be passed explicitly as the
    pass's full denominator whenever candidates were filtered before testing.
    Correcting against only the survivors is the most common way a sweep
    reports significance it has not earned.
    """
    m = n_tests if n_tests is not None else len(p_values)
    if not p_values or m <= 0:
        return BHResult(alpha, m, 0.0, 0)
    ranked = sorted(range(len(p_values)), key=lambda i: p_values[i])
    threshold = 0.0
    k = 0
    for rank, idx in enumerate(ranked, start=1):
        crit = alpha * rank / m
        if p_values[idx] <= crit:
            threshold = crit
            k = rank
    return BHResult(alpha=alpha, n_tests=m, threshold=threshold,
                    n_significant=k, ranked=ranked)


def profit_factor(returns: list) -> float:
    gains = sum(r for r in returns if r > 0)
    losses = -sum(r for r in returns if r < 0)
    if losses <= 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def max_drawdown_from_returns(returns: list) -> float:
    from ..accel import default as accel_default
    equity, cum = [], 0.0
    for r in returns:
        cum += r
        equity.append(cum)
    return accel_default().call("max_drawdown", equity)


def concentration(returns: list, keys: list) -> float:
    """Share of total positive P&L coming from the single largest key.

    A strategy whose profit is one market is one observation wearing a
    strategy's clothes.
    """
    if not returns or len(returns) != len(keys):
        return 0.0
    total = sum(r for r in returns if r > 0)
    if total <= 0:
        return 0.0
    by: dict = {}
    for r, k in zip(returns, keys):
        if r > 0:
            by[k] = by.get(k, 0.0) + r
    return max(by.values()) / total if by else 0.0


def summarize(returns: list) -> dict:
    if not returns:
        return {"n": 0, "expectancy": 0.0, "win_rate": 0.0,
                "profit_factor": 0.0, "max_drawdown": 0.0, "sd": 0.0}
    wins = [r for r in returns if r > 0]
    pf = profit_factor(returns)
    return {
        "n": len(returns),
        "expectancy": round(statistics.fmean(returns), 6),
        "win_rate": round(len(wins) / len(returns), 5),
        "profit_factor": None if pf == float("inf") else round(pf, 4),
        "max_drawdown": round(max_drawdown_from_returns(returns), 5),
        "sd": round(statistics.pstdev(returns), 6) if len(returns) > 1 else 0.0,
        "gross_profit": round(sum(wins), 4),
        "gross_loss": round(-sum(r for r in returns if r < 0), 4),
        "avg_win": round(statistics.fmean(wins), 6) if wins else 0.0,
        "avg_loss": round(statistics.fmean(
            [r for r in returns if r < 0]), 6)
        if any(r < 0 for r in returns) else 0.0,
        "median_win": round(statistics.median(wins), 6) if wins else 0.0,
        "worst": round(min(returns), 6),
        "best": round(max(returns), 6),
    }
