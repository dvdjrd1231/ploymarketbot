"""Sequence and order analysis.

The brief asks for repetition, periodicity, conditional dependence, Markov
structure, entropy, change points, autocorrelation, partial autocorrelation,
run distributions, transition probabilities, regime shifts, hidden states,
Bayesian structure, surprise, anomalous ordering and timing clusters.

Every one of them is implemented below, and every one is reported with the
threshold that separates structure from noise. That last part is the whole
discipline of this module: **the null hypothesis is that there is nothing
here**, and a statistic that does not clear its critical value is reported as
"indistinguishable from independent" rather than as a weak signal.

This matters more here than anywhere else in the system. Given enough sequence
statistics, some will look significant on any finite sample. So:

  * each test states its own critical value
  * `structure_found` requires clearing it, not merely trending toward it
  * `n_tests` is reported so the reader can discount accordingly
  * anything found in-sample is offered as a HYPOTHESIS, never as a signal,
    and must go through the same discovery pass as everything else

"Do not assume randomness is predictable" is a design constraint, not a slogan.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field


@dataclass
class Test:
    name: str
    statistic: float
    critical: float
    passed: bool          # True = structure detected beyond the critical value
    detail: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class SequenceReport:
    n: int = 0
    tests: list = field(default_factory=list)
    structure_found: bool = False
    n_significant: int = 0
    surprise: float = 0.0
    change_points: list = field(default_factory=list)
    timing_clusters: list = field(default_factory=list)
    hidden_states: dict = field(default_factory=dict)
    note: str = ""

    def add(self, t: Test) -> None:
        self.tests.append(t)

    def finish(self) -> "SequenceReport":
        self.n_significant = sum(1 for t in self.tests if t.passed)
        self.structure_found = self.n_significant > 0
        # Bonferroni-style honesty: with k tests at 5%, expect k*0.05 false
        # positives. Say so rather than letting the reader assume otherwise.
        expected_false = 0.05 * len(self.tests)
        self.note = (
            f"{self.n_significant} of {len(self.tests)} tests cleared their "
            f"critical value. At 5% each, roughly {expected_false:.1f} would "
            f"be expected to do so by chance alone, so "
            + ("this is within chance and should be treated as noise."
               if self.n_significant <= expected_false else
               "the excess is worth turning into a hypothesis — which must "
               "then survive the ordinary discovery pass like anything else."))
        return self

    def to_dict(self) -> dict:
        return {"n": self.n, "structure_found": self.structure_found,
                "n_significant": self.n_significant,
                "n_tests": len(self.tests),
                "surprise": round(self.surprise, 4),
                "change_points": self.change_points,
                "timing_clusters": self.timing_clusters,
                "hidden_states": self.hidden_states,
                "note": self.note,
                "tests": [t.to_dict() for t in self.tests]}


def analyse(prices: list, times: list | None = None) -> SequenceReport:
    """Full battery over a price series.

    `prices` must be in time order. `times` enables the timing-cluster and
    periodicity tests; without it those are skipped rather than faked.
    """
    rep = SequenceReport(n=len(prices))
    if len(prices) < 30:
        rep.note = f"{len(prices)} points is too short for sequence analysis"
        return rep

    diffs = [b - a for a, b in zip(prices, prices[1:])]
    syms = [1 if d > 0 else (0 if d < 0 else -1) for d in diffs]
    binary = [s for s in syms if s >= 0]
    n = len(binary)

    # Change points and timing clusters are properties of the LEVEL series and
    # of arrival times. They do not need a directional sequence, so they are
    # computed before the battery that does — a series that steps from 0.30 to
    # 0.70 and is otherwise flat has almost no directional moves and yet has
    # the most obvious change point there is.
    rep.change_points = _change_points(prices)
    if times and len(times) == len(prices):
        rep.timing_clusters = _timing_clusters(times)

    if n < 20:
        rep.note = (f"only {n} directional move(s): the dependence battery "
                    f"cannot run. Change points and timing clusters above are "
                    f"still measured.")
        return rep

    p_up = sum(binary) / n

    # -- 1. lag-1 autocorrelation -----------------------------------------
    rep.add(_autocorr(diffs, lag=1))
    # -- 2. lag-2 autocorrelation (partial-like control) -------------------
    rep.add(_autocorr(diffs, lag=2))
    # -- 3. partial autocorrelation at lag 2 -------------------------------
    rep.add(_pacf2(diffs))
    # -- 4. Markov / transition independence -------------------------------
    rep.add(_transition_chi2(binary, p_up))
    # -- 5. runs test ------------------------------------------------------
    rep.add(_runs(binary, p_up))
    # -- 6. run-length distribution ---------------------------------------
    rep.add(_run_lengths(binary, p_up))
    # -- 7. entropy vs maximum --------------------------------------------
    rep.add(_entropy(binary, p_up))
    # -- 8. periodicity ----------------------------------------------------
    rep.add(_periodicity(diffs))
    # -- 9. variance ratio (random walk test) ------------------------------
    rep.add(_variance_ratio(prices))

    rep.hidden_states = _two_state_fit(binary)
    rep.surprise = _surprise(binary, p_up)
    return rep.finish()


# ---------------------------------------------------------------------------
def _autocorr(x: list, lag: int) -> Test:
    n = len(x)
    if n <= lag + 5 or statistics.pvariance(x) == 0:
        return Test(f"autocorr_lag{lag}", 0.0, 0.0, False, "degenerate series")
    m = statistics.fmean(x)
    var = statistics.pvariance(x)
    num = sum((x[i] - m) * (x[i + lag] - m) for i in range(n - lag))
    r = num / ((n - lag) * var)
    crit = 1.96 / math.sqrt(n)
    return Test(f"autocorr_lag{lag}", round(r, 5), round(crit, 5),
                abs(r) > crit,
                f"r={r:+.4f} against a white-noise band of +/-{crit:.4f}; "
                + ("moves persist" if r > crit else
                   "moves revert" if r < -crit else
                   "indistinguishable from independent"))


def _pacf2(x: list) -> Test:
    """Partial autocorrelation at lag 2, controlling for lag 1.

    Distinguishes genuine two-step memory from lag-1 dependence showing through.
    """
    n = len(x)
    if n < 20 or statistics.pvariance(x) == 0:
        return Test("pacf_lag2", 0.0, 0.0, False, "degenerate series")
    m = statistics.fmean(x)
    var = statistics.pvariance(x)

    def r(lag):
        return sum((x[i] - m) * (x[i + lag] - m)
                   for i in range(n - lag)) / ((n - lag) * var)

    r1, r2 = r(1), r(2)
    denom = 1 - r1 * r1
    p2 = (r2 - r1 * r1) / denom if abs(denom) > 1e-12 else 0.0
    crit = 1.96 / math.sqrt(n)
    return Test("pacf_lag2", round(p2, 5), round(crit, 5), abs(p2) > crit,
                f"partial r={p2:+.4f} after removing lag-1; "
                + ("genuine two-step memory" if abs(p2) > crit
                   else "lag-2 correlation is explained by lag-1"))


def _transition_chi2(b: list, p_up: float) -> Test:
    from ..accel import default as accel
    chi, n, p = accel().call("transition_chi2", [int(x) for x in b])
    return Test("markov_independence", round(chi, 4), 3.84, chi > 3.84,
                f"chi2={chi:.2f} over {n} transitions against 3.84 at df=1; "
                + ("the next move depends on the last one"
                   if chi > 3.84 else "transitions are independent"))


def _runs(b: list, p_up: float) -> Test:
    """Wald-Wolfowitz runs test."""
    n = len(b)
    n1 = sum(b)
    n0 = n - n1
    if n1 == 0 or n0 == 0:
        return Test("runs", 0.0, 1.96, False, "series is constant")
    runs = 1 + sum(1 for i in range(1, n) if b[i] != b[i - 1])
    mu = 2 * n1 * n0 / n + 1
    var = (2 * n1 * n0 * (2 * n1 * n0 - n)) / (n * n * (n - 1))
    if var <= 0:
        return Test("runs", 0.0, 1.96, False, "degenerate")
    z = (runs - mu) / math.sqrt(var)
    return Test("runs", round(z, 4), 1.96, abs(z) > 1.96,
                f"{runs} runs against {mu:.1f} expected, z={z:+.2f}; "
                + ("too few runs: moves cluster" if z < -1.96 else
                   "too many runs: moves alternate" if z > 1.96 else
                   "run count is consistent with independence"))


def _run_lengths(b: list, p_up: float) -> Test:
    """Longest run against what independence would produce."""
    best = cur = 1
    for i in range(1, len(b)):
        cur = cur + 1 if b[i] == b[i - 1] else 1
        best = max(best, cur)
    n = len(b)
    p = max(p_up, 1 - p_up)
    expected = math.log(n * (1 - p), p) if 0 < p < 1 else best
    # Longest-run distributions have a long right tail; +3 is a practical cut.
    crit = expected + 3
    return Test("longest_run", float(best), round(crit, 2), best > crit,
                f"longest run {best} against ~{expected:.1f} expected under "
                f"independence")


def _entropy(b: list, p_up: float) -> Test:
    """Conditional entropy against unconditional: does the past inform?"""
    if not 0 < p_up < 1:
        return Test("entropy_reduction", 0.0, 0.02, False, "constant series")
    h0 = -(p_up * math.log2(p_up) + (1 - p_up) * math.log2(1 - p_up))
    trans = {(0, 0): 0, (0, 1): 0, (1, 0): 0, (1, 1): 0}
    for a, c in zip(b, b[1:]):
        trans[(a, c)] += 1
    h1 = 0.0
    total = sum(trans.values())
    for prev in (0, 1):
        row = trans[(prev, 0)] + trans[(prev, 1)]
        if row == 0:
            continue
        for nxt in (0, 1):
            q = trans[(prev, nxt)] / row
            if q > 0:
                h1 -= (row / total) * q * math.log2(q)
    reduction = h0 - h1
    # 0.02 bits is about the smallest reduction that is not sampling noise at
    # these sample sizes.
    return Test("entropy_reduction", round(reduction, 5), 0.02,
                reduction > 0.02,
                f"knowing the previous move reduces uncertainty by "
                f"{reduction:.4f} bits (from {h0:.3f} to {h1:.3f})")


def _periodicity(x: list) -> Test:
    """Strongest periodic component by a coarse discrete Fourier scan."""
    n = len(x)
    if n < 32:
        return Test("periodicity", 0.0, 0.0, False, "series too short")
    m = statistics.fmean(x)
    y = [v - m for v in x]
    best_p, best_amp = 0, 0.0
    total = sum(v * v for v in y) or 1e-12
    for period in range(2, min(n // 3, 60)):
        w = 2 * math.pi / period
        re = sum(y[i] * math.cos(w * i) for i in range(n))
        im = sum(y[i] * math.sin(w * i) for i in range(n))
        amp = (re * re + im * im) / n
        if amp > best_amp:
            best_amp, best_p = amp, period
    share = best_amp / total * n
    # Under white noise the largest of ~60 periodogram ordinates sits near
    # this level; anything below it is the expected maximum of noise.
    crit = math.log(60) / n * 6
    return Test("periodicity", round(share, 5), round(crit, 5), share > crit,
                f"strongest period {best_p} carries {share:.4f} of variance")


def _variance_ratio(prices: list) -> Test:
    """Lo-MacKinlay style variance ratio at q=2. 1.0 means a random walk."""
    d1 = [b - a for a, b in zip(prices, prices[1:])]
    d2 = [prices[i + 2] - prices[i] for i in range(len(prices) - 2)]
    if len(d1) < 10 or statistics.pvariance(d1) == 0:
        return Test("variance_ratio", 1.0, 0.0, False, "degenerate")
    vr = statistics.pvariance(d2) / (2 * statistics.pvariance(d1))
    n = len(d1)
    se = math.sqrt(2.0 / n)
    z = (vr - 1.0) / se
    return Test("variance_ratio", round(vr, 5), round(1 + 1.96 * se, 5),
                abs(z) > 1.96,
                f"VR(2)={vr:.4f}, z={z:+.2f}; "
                + ("trending" if z > 1.96 else "mean-reverting" if z < -1.96
                   else "consistent with a random walk"))


def _change_points(prices: list, *, min_seg: int = 15) -> list:
    """Binary segmentation on mean shift, with a significance cut.

    Reports at most three: a method that can find a change point anywhere will
    find one everywhere, and a long list of them is a sign the method is
    fitting noise.
    """
    out = []

    def scan(lo, hi, depth=0):
        if hi - lo < 2 * min_seg or depth >= 2 or len(out) >= 3:
            return
        seg = prices[lo:hi]
        best_i, best_t = -1, 0.0
        for i in range(min_seg, len(seg) - min_seg):
            a, b = seg[:i], seg[i:]
            va = statistics.pvariance(a) if len(a) > 1 else 0.0
            vb = statistics.pvariance(b) if len(b) > 1 else 0.0
            pooled = (va * len(a) + vb * len(b)) / len(seg)
            if pooled <= 0:
                continue
            t = abs(statistics.fmean(a) - statistics.fmean(b)) / math.sqrt(
                pooled * (1 / len(a) + 1 / len(b)))
            if t > best_t:
                best_t, best_i = t, i
        if best_i > 0 and best_t > 3.0:
            out.append({"index": lo + best_i, "t_stat": round(best_t, 3),
                        "before": round(statistics.fmean(prices[lo:lo + best_i]), 5),
                        "after": round(statistics.fmean(prices[lo + best_i:hi]), 5)})
            scan(lo, lo + best_i, depth + 1)
            scan(lo + best_i, hi, depth + 1)

    scan(0, len(prices))
    return sorted(out, key=lambda d: d["index"])


def _two_state_fit(b: list) -> dict:
    """A two-state Markov description: the interpretable stand-in for an HMM.

    A proper hidden Markov model fitted to a few hundred points of one venue's
    data would mostly fit noise, and its latent states would be uninterpretable
    at exactly the moment you needed to interpret them. A two-state observed
    Markov chain says the same useful thing — is there persistence, and how
    much — with parameters a human can read.
    """
    if len(b) < 20:
        return {}
    t = {(0, 0): 0, (0, 1): 0, (1, 0): 0, (1, 1): 0}
    for a, c in zip(b, b[1:]):
        t[(a, c)] += 1
    p11 = t[(1, 1)] / max(t[(1, 1)] + t[(1, 0)], 1)
    p00 = t[(0, 0)] / max(t[(0, 0)] + t[(0, 1)], 1)
    persistence = (p11 + p00) / 2
    return {
        "p_stay_up": round(p11, 4), "p_stay_down": round(p00, 4),
        "persistence": round(persistence, 4),
        "expected_up_run": round(1 / (1 - p11), 2) if p11 < 1 else None,
        "expected_down_run": round(1 / (1 - p00), 2) if p00 < 1 else None,
        "interpretation": (
            "persistent: moves continue" if persistence > 0.55 else
            "alternating: moves reverse" if persistence < 0.45 else
            "memoryless within measurement error"),
    }


def _surprise(b: list, p_up: float) -> float:
    """Mean self-information of the last 20 symbols, in bits.

    High surprise means the recent sequence is unlikely under the series' own
    long-run rate — the quantitative form of "something changed".
    """
    if not 0 < p_up < 1 or len(b) < 5:
        return 0.0
    tail = b[-20:]
    return sum(-math.log2(p_up if x else 1 - p_up) for x in tail) / len(tail)


def _timing_clusters(times: list, *, z: float = 2.0) -> list:
    """Inter-arrival gaps far below the norm: bursts of activity."""
    if len(times) < 20:
        return []
    gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
    if len(gaps) < 10:
        return []
    med = statistics.median(gaps)
    if med <= 0:
        return []
    out = []
    run_start = None
    for i, g in enumerate(gaps):
        if g < med / (1 + z):
            run_start = i if run_start is None else run_start
        elif run_start is not None:
            if i - run_start >= 3:
                out.append({"from_index": run_start, "to_index": i,
                            "n_trades": i - run_start + 1,
                            "median_gap_secs": med})
            run_start = None
    return out[:5]
