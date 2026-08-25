"""Fast in-sample screening.

The problem, stated as arithmetic. A pass generates a few thousand hypotheses
and the in-sample window holds ~82,000 observations. Evaluating each hypothesis
by scanning every row is hundreds of millions of Python-level comparisons —
minutes to hours, which makes the research loop unusable and therefore unused.

The fix is not a faster loop, it is a better representation. Every rule in this
system is a threshold on one feature, so a rule's admitted set can be
precomputed ONCE as a set of row indices. A hypothesis is then a set
intersection, which CPython performs in C.

    build 220 rule sets over the window      one pass, O(rules x rows)
    screen a hypothesis                      set & set, C speed
    score the survivors                      O(admitted), not O(rows)

Two honesty constraints on this optimisation:

**Screening may sample; testing may not.** Rule sets over the full window cost
memory proportional to rules x rows, so screening runs on a bounded stratified
sample. That is a compute decision about a FILTER. Every candidate that
survives is then re-evaluated on the complete in-sample and out-of-sample
windows by `backtest.evaluate`, which scans every row. The sample size is
recorded in the pass notes so the reader knows what was filtered and what was
measured.

**Sampling is stratified by time**, not random. A random sample of a tape where
the same market appears many times over-represents busy markets and busy weeks;
an evenly-spaced sample preserves the era mix, which is what the screen is
trying to be representative of.

This is also the one place in V3 where a Rust kernel would clearly earn its
keep — see `docs/ENGINE-PERFORMANCE.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from .baseline import build as build_matched
from .hypothesis import Hypothesis, Rule
from .matrix import Matrix


@dataclass
class ScreenResult:
    kept: list = field(default_factory=list)   # (Hypothesis, n, excess, absolute)
    excess_only: int = 0        # beat their price band, but still lost money
    absolute_only: int = 0      # made money, but only by picking the band
    evaluated: int = 0
    sample_rows: int = 0
    window_rows: int = 0
    stride: int = 1
    baseline: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return {"evaluated": self.evaluated, "kept": len(self.kept),
                "excess_positive_but_lost_money": self.excess_only,
                "profitable_but_no_alpha": self.absolute_only,
                "sample_rows": self.sample_rows,
                "window_rows": self.window_rows, "stride": self.stride,
                "baseline_expectancy": round(self.baseline, 6),
                "note": self.note}


def _sample_indices(lo: int, hi: int, max_rows: int) -> tuple:
    """Evenly spaced rows across the window. Deterministic."""
    n = hi - lo
    if n <= max_rows:
        return tuple(range(lo, hi)), 1
    stride = (n // max_rows) + 1
    return tuple(range(lo, hi, stride)), stride


def screen(m: Matrix, hypotheses: list, st: Settings, *, lo: int, hi: int,
           max_sample_rows: int = 40_000, min_n: int = 25,
           progress=None) -> ScreenResult:
    res = ScreenResult(window_rows=hi - lo)
    rows, stride = _sample_indices(lo, hi, max_sample_rows)
    res.sample_rows = len(rows)
    res.stride = stride
    if stride > 1:
        res.note = (f"screened on {len(rows)} of {hi - lo} in-sample rows "
                    f"(every {stride}th, evenly spaced in time). Screening is "
                    f"a filter; every survivor is re-measured on the complete "
                    f"in-sample and out-of-sample windows.")
    if not rows:
        res.note = "empty in-sample window"
        return res

    # TWO criteria, both required, because they answer different questions and
    # on this data they frequently disagree:
    #
    #   absolute expectancy > 0   would this have made money?
    #   matched excess     > 0    ...for a reason other than the price band?
    #
    # The tape's mean raw return is about -0.20: longshots are systematically
    # overpriced, and buying one that loses returns -100% while one that wins
    # returns +1900%. So a rule can beat every peer in its own price band and
    # still lose money in absolute terms — the first matched screen produced
    # exactly that, a rule with +0.224 excess and -0.084 actual return.
    # Screening on either criterion alone fills the finalist list with
    # candidates the validation ladder will then reject, wasting the entire
    # downstream budget.
    mb = build_matched(m, st, min(rows), max(rows) + 1, rows=rows)
    ret: dict = {}
    raw: dict = {}
    for i in rows:
        e = mb.excess(i)
        if e is not None:
            ret[i] = e
            raw[i] = mb.ret[i]
    if not ret:
        res.note = ("no row in the window has a comparable price-band/week "
                    "peer group; nothing can be screened")
        return res
    # By construction the mean leave-one-out excess over a whole bucket is ~0,
    # so the screening baseline is zero and `alpha` below IS the excess.
    res.baseline = 0.0
    valid = set(ret)

    # -- precompute one index set per distinct rule -----------------------
    rule_sets: dict = {}

    def set_for(r: Rule) -> set:
        key = (r.feature, r.op, r.value)
        s = rule_sets.get(key)
        if s is None:
            col = m.cols[r.feature]
            if r.op == "ge":
                s = {i for i in valid if col[i] >= r.value}
            else:
                s = {i for i in valid if col[i] <= r.value}
            rule_sets[key] = s
        return s

    for h in hypotheses:
        for r in h.rules:
            set_for(r)
    if progress:
        progress(f"  built {len(rule_sets)} rule index sets over "
                 f"{len(valid)} rows")

    # -- screen ------------------------------------------------------------
    for k, h in enumerate(hypotheses):
        res.evaluated += 1
        sets = [set_for(r) for r in h.rules]
        sets.sort(key=len)                     # intersect smallest-first
        admitted = sets[0]
        for s in sets[1:]:
            admitted = admitted & s
            if len(admitted) < min_n:
                break
        n = len(admitted)
        if n < min_n:
            continue
        s = 0.0
        sr = 0.0
        for i in admitted:
            s += ret[i]
            sr += raw[i]
        alpha = s / n
        absolute = sr / n
        if alpha > 0 and absolute > 0:
            res.kept.append((h, n, alpha, absolute))
        elif alpha > 0:
            res.excess_only += 1
        elif absolute > 0:
            res.absolute_only += 1
        if progress and k and k % 2000 == 0:
            progress(f"  screened {k}/{len(hypotheses)}, "
                     f"{len(res.kept)} alive")

    res.kept.sort(key=lambda kv: -kv[2])   # by matched excess
    return res
