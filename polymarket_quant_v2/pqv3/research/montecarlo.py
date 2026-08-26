"""§17 / §1 — Monte Carlo over the paths a strategy could have taken.

A backtest reports the one ordering of trades that history happened to deal.
That single path answers "what did this return" and cannot answer the question
§1 actually poses — maximise long-horizon risk-adjusted GEOMETRIC growth, with
probability of ruin as a first-class term — because ruin is a property of the
ORDER of the returns, not of their multiset.

The demonstration is arithmetic. Take twenty trades: nineteen at +6% and one at
-60%. Every ordering ends at the same terminal wealth. But the ordering that
deals the -60% first, at full size, may put the account under the hard stop
before the nineteen winners ever arrive, and a halted account does not receive
them. The backtest's Sharpe, expectancy, profit factor and total return are all
identical across every one of those orderings. The probability of ruin is not.

So this resamples the observed per-trade returns into many alternative
orderings, compounds each one at the configured bankroll, and reports the
DISTRIBUTION of terminal wealth, maximum drawdown, and ruin.

TWO RESAMPLERS, and choosing between them is a real decision rather than a
default:

    iid     draws trades independently. Correct only if trades are genuinely
            independent. They usually are not — losses cluster, because the
            conditions that made one trade wrong are still there for the next —
            and under clustering this UNDERSTATES drawdown and ruin, which is
            the dangerous direction to be wrong in.

    block   draws contiguous runs of about n**(1/3) trades, so a losing streak
            can survive resampling intact. This is the default, and where the
            two disagree the honest reading is the worse one.

WHAT THIS CANNOT DO, stated because the output looks more authoritative than it
is: resampling cannot produce a market condition absent from the sample. If the
observed trades never met a liquidity crisis, no ordering of them contains one,
and the 5th-percentile outcome here is the 5th percentile of a benign world.
This measures SEQUENCING risk given the observed return distribution. It is not
a forecast, and every field it returns is labelled accordingly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .surrogate import Rng


@dataclass
class PathStats:
    paths: int = 0
    trades_per_path: int = 0
    resampler: str = ""
    starting_capital: float = 0.0

    # Terminal wealth
    median_final: float = 0.0
    p05_final: float = 0.0
    p25_final: float = 0.0
    p75_final: float = 0.0
    p95_final: float = 0.0
    mean_final: float = 0.0

    # Growth
    median_log_growth_per_trade: float = 0.0
    prob_profit: float = 0.0

    # Risk
    median_max_drawdown: float = 0.0
    p95_max_drawdown: float = 0.0
    prob_ruin: float = 0.0
    prob_hard_stop: float = 0.0
    ruin_threshold: float = 0.0
    hard_stop_drawdown: float = 0.0

    observed_final: float = 0.0
    observed_percentile: float = 0.0

    warnings: list = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _pct(sorted_xs: list, q: float) -> float:
    """Linear-interpolated percentile. Nearest-rank would quantise the tail."""
    if not sorted_xs:
        return 0.0
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    pos = q * (len(sorted_xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_xs) - 1)
    frac = pos - lo
    return sorted_xs[lo] * (1 - frac) + sorted_xs[hi] * frac


def _walk(returns: list, *, start: float, fraction: float,
          ruin_at: float, stop_dd: float) -> tuple:
    """Compound one ordering. Returns (final, max_drawdown, ruined, stopped).

    Fractional betting, so the compounding is multiplicative and a 100% loss on
    one trade cannot take the account to zero unless `fraction` is 1.0 — which
    is the whole reason a capital model exists. Trading STOPS at the hard-stop
    drawdown rather than continuing: an account that has halted does not
    receive the trades that came after, and simulating those trades anyway is
    the single most common way a Monte Carlo understates ruin.
    """
    equity = start
    peak = start
    worst = 0.0
    ruined = stopped = False
    for r in returns:
        equity *= (1.0 + fraction * r)
        if equity > peak:
            peak = equity
        dd = 0.0 if peak <= 0 else (peak - equity) / peak
        if dd > worst:
            worst = dd
        if equity <= ruin_at:
            ruined = stopped = True
            break
        if stop_dd > 0 and dd >= stop_dd:
            stopped = True
            break
    return equity, worst, ruined, stopped


def simulate(returns: list, *, starting_capital: float = 100.0,
             fraction: float = 1.0, paths: int = 2000,
             resampler: str = "block", seed: int = 20260825,
             ruin_fraction: float = 0.20, hard_stop_drawdown: float = 0.25,
             block_len: int = 0, min_trades: int = 20) -> PathStats:
    """Resample orderings of `returns` and report the distribution of outcomes.

    `returns` are per-trade fractional returns on the amount risked, e.g. 0.06
    for +6%. `fraction` is how much of equity each trade risks — pass the
    capital model's per-trade cap to simulate the system as configured, or 1.0
    to see the raw strategy compounded.
    """
    n = len(returns)
    st = PathStats(paths=paths, trades_per_path=n, resampler=resampler,
                   starting_capital=starting_capital,
                   ruin_threshold=round(starting_capital * ruin_fraction, 4),
                   hard_stop_drawdown=hard_stop_drawdown)
    if n < min_trades:
        st.warnings.append(
            f"{n} trades is below the {min_trades}-trade floor. Resampling a "
            f"handful of returns produces a distribution of the sampling "
            f"noise, not of the strategy")
        st.note = "INSUFFICIENT_EVIDENCE — §33"
        return st

    returns = [float(r) for r in returns]
    rng = Rng(seed)
    L = block_len or max(2, int(round(n ** (1 / 3))))
    ruin_at = starting_capital * ruin_fraction

    finals, dds = [], []
    ruins = stops = 0
    for _ in range(paths):
        if resampler == "iid":
            order = [returns[rng.randrange(n)] for _ in range(n)]
        elif resampler == "shuffle":
            order = rng.shuffled(returns)
        else:
            order = []
            while len(order) < n:
                s = rng.randrange(max(1, n - L + 1))
                order.extend(returns[s:s + L])
            order = order[:n]
        f, dd, ruined, stopped = _walk(
            order, start=starting_capital, fraction=fraction,
            ruin_at=ruin_at, stop_dd=hard_stop_drawdown)
        finals.append(f)
        dds.append(dd)
        ruins += ruined
        stops += stopped

    finals.sort()
    dds.sort()
    st.median_final = round(_pct(finals, 0.50), 4)
    st.p05_final = round(_pct(finals, 0.05), 4)
    st.p25_final = round(_pct(finals, 0.25), 4)
    st.p75_final = round(_pct(finals, 0.75), 4)
    st.p95_final = round(_pct(finals, 0.95), 4)
    st.mean_final = round(sum(finals) / len(finals), 4)
    st.median_max_drawdown = round(_pct(dds, 0.50), 5)
    st.p95_max_drawdown = round(_pct(dds, 0.95), 5)
    st.prob_ruin = round(ruins / paths, 5)
    st.prob_hard_stop = round(stops / paths, 5)
    st.prob_profit = round(
        sum(1 for f in finals if f > starting_capital) / paths, 5)

    import math
    med = st.median_final
    st.median_log_growth_per_trade = round(
        math.log(med / starting_capital) / n, 6) if med > 0 else 0.0

    obs = _walk(returns, start=starting_capital, fraction=fraction,
                ruin_at=ruin_at, stop_dd=hard_stop_drawdown)[0]
    st.observed_final = round(obs, 4)
    st.observed_percentile = round(
        sum(1 for f in finals if f <= obs) / paths, 4)

    st.note = (
        f"{paths} resampled orderings of the SAME {n} trades, {resampler} "
        f"resampler, {fraction:.0%} of equity per trade. Every ordering has "
        f"identical expectancy, win rate and profit factor — the backtest "
        f"cannot tell them apart. They differ in drawdown "
        f"({st.median_max_drawdown:.1%} median, {st.p95_max_drawdown:.1%} at "
        f"the 95th) and in whether the account survived: "
        f"{st.prob_hard_stop:.1%} of paths hit the {hard_stop_drawdown:.0%} "
        f"hard stop and stopped trading, {st.prob_ruin:.1%} fell to the ruin "
        f"threshold of ${st.ruin_threshold:.2f}. "
        f"History's own ordering finished at ${st.observed_final:.2f}, the "
        f"{st.observed_percentile:.0%} percentile of this distribution — if "
        f"that is far above the median, the backtest was lucky in its "
        f"sequencing and not only in its selection. "
        f"NOT A FORECAST: resampling cannot produce a market condition absent "
        f"from these trades.")
    return st


def compare_resamplers(returns: list, **kw) -> dict:
    """Run both resamplers. Where they disagree, the worse one is the answer.

    The gap between them measures how much of the strategy's apparent safety
    depends on assuming its losses do not cluster. That assumption is usually
    false and is never free.
    """
    kw.pop("resampler", None)
    iid = simulate(returns, resampler="iid", **kw)
    blk = simulate(returns, resampler="block", **kw)
    d_ruin = blk.prob_ruin - iid.prob_ruin
    d_dd = blk.p95_max_drawdown - iid.p95_max_drawdown
    return {
        "iid": iid.to_dict(), "block": blk.to_dict(),
        "clustering_cost": {
            "extra_ruin_probability": round(d_ruin, 5),
            "extra_p95_drawdown": round(d_dd, 5)},
        "reading": (
            "the two agree; this strategy's risk profile does not depend on "
            "whether its losses cluster"
            if abs(d_ruin) < 0.01 and abs(d_dd) < 0.02 else
            f"allowing losses to cluster raises the probability of ruin by "
            f"{d_ruin:+.1%} and the 95th-percentile drawdown by {d_dd:+.1%}. "
            f"Trades are rarely independent, so the block figure is the one to "
            f"plan against"),
        "note": ("iid resampling assumes each trade is independent of the "
                 "last. When that is false it understates drawdown and ruin, "
                 "which is the dangerous direction")}
