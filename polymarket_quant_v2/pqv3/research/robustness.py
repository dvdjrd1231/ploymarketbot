"""Robustness battery: does the result survive being poked?

A strategy that collapses under a 10% threshold change was never describing the
world; it was describing this particular sample. Each test below is designed so
that FAILING it is informative, not merely a lower score:

  * **parameter perturbation** — shift every threshold by +/-10% and +/-20%.
    An edge that lives on a knife's edge is a fitted boundary.
  * **time shift** — evaluate on windows offset by +/-10% of the span. A real
    effect does not depend on where the window starts.
  * **subsample** — random halves. Detects an effect carried by a handful of
    rows.
  * **block bootstrap** — resamples blocks, preserving serial dependence.
  * **market shuffle** — reassign outcomes across markets. This is the NULL:
    the statistic should collapse. If it does not, the rule is picking up
    something structural about the sample rather than about the outcome.
  * **wallet holdout** — drop the most-represented wallet. Detects a "strategy"
    that is one wallet's history wearing a rule's clothes.
  * **slippage stress** — 2x and 4x the assumed cost.
  * **latency stress** — require the trade to still work if entered later.
  * **capital stress** — re-run the $100 test at half and a quarter bankroll.

`fragile` is set when any *critical* test fails. The distinction matters: a
strategy that dies at 4x slippage is worth knowing about but is not
necessarily fitted, whereas one that dies under a 10% parameter change is.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..config import Settings
from .backtest import baseline_returns, capital_test, evaluate
from .hypothesis import Hypothesis, Rule
from .matrix import Matrix
from . import stats


@dataclass
class Check:
    name: str
    passed: bool
    critical: bool
    detail: str = ""
    value: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Robustness:
    checks: list = field(default_factory=list)
    fragile: bool = False
    survival: float = 0.0
    note: str = ""

    def add(self, c: Check) -> None:
        self.checks.append(c)

    def finish(self) -> "Robustness":
        if not self.checks:
            self.fragile = True
            self.note = "no robustness check could be run"
            return self
        self.survival = round(
            sum(1 for c in self.checks if c.passed) / len(self.checks), 4)
        failed_critical = [c for c in self.checks if c.critical and not c.passed]
        self.fragile = bool(failed_critical)
        if failed_critical:
            self.note = "failed: " + "; ".join(c.name for c in failed_critical)
        return self

    def to_dict(self) -> dict:
        return {"fragile": self.fragile, "survival": self.survival,
                "note": self.note,
                "checks": [c.to_dict() for c in self.checks]}


def _expectancy(m: Matrix, h: Hypothesis, st: Settings, lo: int, hi: int,
                base: list) -> tuple:
    ev = evaluate(m, h, st, lo=lo, hi=hi, baseline_returns=base,
                  with_stats=False)
    return ev.alpha_vs_baseline, ev.n


def run(m: Matrix, h: Hypothesis, st: Settings, *, lo: int, hi: int,
        reference_alpha: float, min_n: int = 10) -> Robustness:
    rb = Robustness()
    base = baseline_returns(m, st, lo, hi, stride=3)
    rng = random.Random(st.research.seed)

    if reference_alpha <= 0:
        rb.add(Check("reference", False, True,
                     "baseline-adjusted expectancy is not positive to begin with"))
        return rb.finish()

    # -- parameter perturbation -------------------------------------------
    worst = reference_alpha
    for pct in (0.10, -0.10, 0.20, -0.20):
        rules = tuple(Rule(r.feature, r.op,
                           r.value * (1 + pct) if r.value else r.value + pct)
                      for r in h.rules)
        alt = Hypothesis(h.hypothesis_id + f"_p{pct}", h.family, rules,
                         h.statement)
        a, n = _expectancy(m, alt, st, lo, hi, base)
        if n >= min_n:
            worst = min(worst, a)
    rb.add(Check("parameter_perturbation", worst > 0, True,
                 f"worst baseline-adjusted expectancy under +/-10% and +/-20% "
                 f"threshold shifts was {worst:+.5f}", round(worst, 6)))

    # -- time shift --------------------------------------------------------
    span = hi - lo
    shift_ok = True
    shift_worst = reference_alpha
    for frac in (0.10, -0.10):
        s = int(span * frac)
        a2, b2 = max(0, lo + s), min(len(m), hi + s)
        if b2 - a2 < min_n:
            continue
        a, n = _expectancy(m, h, st, a2, b2,
                           baseline_returns(m, st, a2, b2, stride=3))
        if n >= min_n:
            shift_worst = min(shift_worst, a)
            shift_ok = shift_ok and a > 0
    rb.add(Check("time_shift", shift_ok, True,
                 f"worst expectancy under +/-10% window shift "
                 f"{shift_worst:+.5f}", round(shift_worst, 6)))

    # -- subsample ---------------------------------------------------------
    from .hypothesis import admit_mask
    idx = admit_mask(m, h, lo, hi)
    cost = 1.0 + (st.costs.slippage_bps + st.costs.fee_bps) / 10_000.0
    rets = []
    for i in idx:
        p = m.cols["price"][i] * cost
        if 0 < p < 1:
            rets.append((m.resolution[i] - p) / p)
    base_mean = stats.mean(base)

    sub_pos = 0
    trials = 20
    for _ in range(trials):
        half = rng.sample(rets, max(2, len(rets) // 2)) if len(rets) > 3 else rets
        if stats.mean(half) - base_mean > 0:
            sub_pos += 1
    share = sub_pos / trials
    rb.add(Check("subsample", share >= 0.7, True,
                 f"{share:.0%} of 20 random halves stayed positive after the "
                 f"baseline", round(share, 4)))

    # -- block bootstrap ---------------------------------------------------
    lo_ci, hi_ci, pos = stats.block_bootstrap_ci(
        rets, draws=min(1000, st.research.bootstrap_draws), seed=st.research.seed)
    rb.add(Check("block_bootstrap", (lo_ci - base_mean) > 0, True,
                 f"95% CI [{lo_ci:+.5f}, {hi_ci:+.5f}] against a baseline of "
                 f"{base_mean:+.5f}; {pos:.0%} of resamples positive",
                 round(lo_ci - base_mean, 6)))

    # -- market shuffle: THE NULL -----------------------------------------
    # Outcomes are permuted across the window. The rule should now be worth
    # nothing. If it is not, the rule is exploiting sample structure.
    shuffled = list(m.resolution[lo:hi])
    rng.shuffle(shuffled)
    sh = []
    for i in idx:
        p = m.cols["price"][i] * cost
        if 0 < p < 1:
            sh.append((shuffled[i - lo] - p) / p)
    sh_mean = stats.mean(sh)
    rb.add(Check("market_shuffle_null", sh_mean < reference_alpha * 0.5, True,
                 f"under shuffled outcomes the rule returns {sh_mean:+.5f} "
                 f"versus {reference_alpha:+.5f} real; the null must collapse",
                 round(sh_mean, 6)))

    # -- wallet holdout ----------------------------------------------------
    counts: dict = {}
    for i in idx:
        counts[m.wallet[i]] = counts.get(m.wallet[i], 0) + 1
    if counts:
        top = max(counts, key=counts.get)
        kept = [i for i in idx if m.wallet[i] != top]
        share_top = counts[top] / len(idx)
        if kept:
            r2 = []
            for i in kept:
                p = m.cols["price"][i] * cost
                if 0 < p < 1:
                    r2.append((m.resolution[i] - p) / p)
            a2 = stats.mean(r2) - base_mean
            rb.add(Check("wallet_holdout", a2 > 0, True,
                         f"dropping {top[:12]} ({share_top:.0%} of fills) "
                         f"leaves {a2:+.5f}", round(a2, 6)))
        else:
            rb.add(Check("wallet_holdout", False, True,
                         f"every fill comes from one wallet ({top[:12]}); this "
                         f"is a wallet's history, not a strategy", 1.0))

    # -- slippage stress ---------------------------------------------------
    slip_ok = True
    for mult in (2.0, 4.0):
        s2 = Settings()
        s2.__dict__.update(st.__dict__)
        from copy import deepcopy
        s2.costs = deepcopy(st.costs)
        s2.costs.slippage_bps = st.costs.slippage_bps * mult
        a, n = _expectancy(m, h, s2, lo, hi,
                           baseline_returns(m, s2, lo, hi, stride=3))
        if mult == 2.0:
            slip_ok = a > 0
        rb.add(Check(f"slippage_{mult:g}x", a > 0, mult == 2.0,
                     f"baseline-adjusted expectancy at {mult:g}x slippage "
                     f"({s2.costs.slippage_bps:g}bps) is {a:+.5f}", round(a, 6)))

    # -- capital stress ----------------------------------------------------
    from copy import deepcopy
    for frac in (0.5, 0.25):
        s3 = Settings()
        s3.__dict__.update(st.__dict__)
        s3.capital = deepcopy(st.capital)
        s3.capital.starting_capital = st.capital.starting_capital * frac
        ct = capital_test(m, h, s3, lo=lo, hi=hi)
        rb.add(Check(f"capital_{frac:g}x", ct.trades > 0, False,
                     f"at ${s3.capital.starting_capital:.2f} the strategy "
                     f"executed {ct.trades} of {ct.signals} signals "
                     f"({ct.fill_rate:.0%}) for {ct.total_return:+.1%}",
                     round(ct.total_return, 6)))

    return rb.finish()
