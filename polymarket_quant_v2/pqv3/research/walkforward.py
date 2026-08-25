"""Walk-forward validation.

TRAIN -> VALIDATE -> TEST -> ROLL FORWARD -> REPEAT, and the final test window
is never optimised on.

The property that matters is not "we used several folds" but **the folds are
contiguous in time and always ordered train-before-test**. A k-fold split that
shuffles rows would place a market's later trades in training and its earlier
trades in test, which leaks the outcome backwards — on a prediction market the
same token appears many times, so shuffling is not a mild approximation, it is
the whole answer.

Two schedules, because they answer different questions:

  * **rolling** — fixed-width train window. Detects a strategy that only worked
    in one era, because an old edge falls out of the window.
  * **expanding** — train from the start. Detects a strategy that needs a long
    history, and is the honest choice when data is scarce.

A strategy positive in fewer than half its folds is `UNSTABLE`, regardless of
how good its aggregate looks. An aggregate is one number; the folds show
whether that number is a description or an average of two opposite regimes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from .backtest import Evaluation, baseline_returns, evaluate
from .hypothesis import Hypothesis
from .matrix import Matrix


@dataclass
class Fold:
    index: int
    train_from: int
    train_to: int
    test_from: int
    test_to: int
    n: int = 0
    expectancy: float = 0.0
    win_rate: float = 0.0
    alpha_vs_baseline: float = 0.0
    positive: bool = False

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class WalkForward:
    folds: list = field(default_factory=list)
    schedule: str = "rolling"
    n_folds: int = 0
    n_evaluable: int = 0
    positive_share: float = 0.0
    mean_expectancy: float = 0.0
    worst_fold: float = 0.0
    stable: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return {"schedule": self.schedule, "n_folds": self.n_folds,
                "n_evaluable": self.n_evaluable,
                "positive_share": self.positive_share,
                "mean_expectancy": self.mean_expectancy,
                "worst_fold": self.worst_fold, "stable": self.stable,
                "note": self.note,
                "folds": [f.to_dict() for f in self.folds]}


def run(m: Matrix, h: Hypothesis, st: Settings, *, folds: int = 0,
        schedule: str = "expanding", min_fold_n: int = 8) -> WalkForward:
    folds = folds or st.research.walkforward_folds
    wf = WalkForward(schedule=schedule, n_folds=folds)
    lo_ts, hi_ts = m.time_bounds()
    if not hi_ts or hi_ts <= lo_ts:
        wf.note = "no usable time span"
        return wf

    span = hi_ts - lo_ts
    # Reserve the first slice purely for training: a fold with no history
    # before it is not a walk-forward fold, it is a random sample.
    warmup = span // (folds + 1)
    step = (span - warmup) // folds

    for k in range(folds):
        test_from = lo_ts + warmup + k * step
        test_to = test_from + step if k < folds - 1 else hi_ts + 1
        train_from = lo_ts if schedule == "expanding" else max(
            lo_ts, test_from - warmup - step)
        train_to = test_from

        a, b = m.index_range(test_from, test_to)
        f = Fold(index=k, train_from=train_from, train_to=train_to,
                 test_from=test_from, test_to=test_to)
        if b - a < min_fold_n:
            wf.folds.append(f)
            continue

        base = baseline_returns(m, st, a, b, stride=3)
        ev = evaluate(m, h, st, lo=a, hi=b, baseline_returns=base,
                      with_stats=False)
        f.n = ev.n
        if ev.n >= min_fold_n:
            f.expectancy = ev.expectancy
            f.win_rate = ev.win_rate
            f.alpha_vs_baseline = ev.alpha_vs_baseline
            # Positive means positive AFTER the baseline. A fold that merely
            # rode the favourite-longshot bias is not evidence for the rule.
            f.positive = ev.alpha_vs_baseline > 0
            wf.n_evaluable += 1
        wf.folds.append(f)

    scored = [f for f in wf.folds if f.n >= min_fold_n]
    if not scored:
        wf.note = (f"no fold reached {min_fold_n} fills; the strategy is too "
                   f"selective for walk-forward on this span")
        return wf

    wf.positive_share = round(
        sum(1 for f in scored if f.positive) / len(scored), 4)
    wf.mean_expectancy = round(
        sum(f.alpha_vs_baseline for f in scored) / len(scored), 6)
    wf.worst_fold = round(min(f.alpha_vs_baseline for f in scored), 6)
    wf.stable = wf.positive_share >= st.research.min_walkforward_positive
    if not wf.stable:
        wf.note = (f"positive in only {wf.positive_share:.0%} of "
                   f"{len(scored)} evaluable folds")
    return wf
