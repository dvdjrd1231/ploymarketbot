"""§11 — nonlinear dependence: mutual information, transfer entropy, lead-lag.

Correlation answers one question: is Y a straight line in X. Everything §11
asks for lives in the space that question cannot see — a signal that only fires
in the tails, a relationship that reverses at the midpoint, a dependence that
exists in the second moment and nowhere in the first. Mutual information sees
all of those, and transfer entropy sees them with a direction attached.

They also come with a failure mode severe enough that the estimator is nearly
useless without the machinery around it, which is why this module is mostly
machinery:

  1. MI IS BIASED UPWARD AND CANNOT BE NEGATIVE. Two independent series of 300
     samples in a 5x5 grid produce an estimated MI near 0.05 nats every single
     time. A reader who has been handed "MI = 0.05" has been handed noise
     wearing a number's clothes. Everything here is therefore reported against
     a surrogate null, and `mi()` alone is deliberately not the public entry
     point — `mutual_information()` is, and it refuses to return without one.

  2. THE ANSWER DEPENDS ON THE BINNING. Two bins find a monotone relationship
     and miss a U-shape; ten bins find a U-shape and also find sixteen
     relationships that are not there. So every result is computed across a
     grid of bin counts and reports whether the verdict is stable across it.
     A finding that exists at 6 bins and vanishes at 4 and 8 is a binning
     artefact, and this module says so rather than reporting the 6.

  3. THE NULL MUST PRESERVE AUTOCORRELATION. Two independent random walks
     share enormous mutual information, because each is highly informative
     about its own past and they are sampled over the same clock. Testing
     against a shuffled null calls that a discovery. Every test on a time
     series here uses cyclic-shift surrogates, which keep each series' own
     memory intact and destroy only the alignment between them.

TRANSFER ENTROPY is exactly the conditional mutual information
I(Y_{t+1} ; X_t | Y_t): how much X's present says about Y's next step that Y's
own present does not already say. Implementing it as a wrapper over one CMI
estimator rather than as its own formula is not a shortcut — it is the reason
the conditioning is provably right, since the conditioning is what separates
transfer entropy from a lagged correlation.

Nothing in this module is a trading signal. It is a screen: it says where to
look, and everything it points at still has to survive `pqv3 discover`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .surrogate import Rng, SurrogateTest, cyclic_shift, shuffle, test

# Bin counts swept for the stability check. Small on purpose: the number of
# cells grows as B**2 for MI and B**3 for CMI, and a cell that never fills
# contributes bias without contributing information.
BIN_GRID = (3, 4, 5, 6)
MIN_SAMPLES_PER_CELL = 5.0


# ---------------------------------------------------------------------------
# Binning
# ---------------------------------------------------------------------------

def quantise(xs: list, bins: int) -> list:
    """Equal-frequency binning. Ties share a bin, which shrinks the grid.

    Equal-frequency rather than equal-width because a price series spends most
    of its life in a narrow band: equal-width binning puts 95% of the mass in
    one cell and estimates a mutual information of approximately zero for
    everything, including relationships that are really there.
    """
    n = len(xs)
    if n == 0 or bins < 2:
        return [0] * n
    order = sorted(range(n), key=lambda i: xs[i])
    out = [0] * n
    # Assign by rank, then collapse ties onto the bin of their first
    # occurrence: a value must not land in two different bins.
    edge_of_rank = [min(bins - 1, (r * bins) // n) for r in range(n)]
    seen: dict = {}
    for r, i in enumerate(order):
        v = xs[i]
        if v in seen:
            out[i] = seen[v]
        else:
            seen[v] = edge_of_rank[r]
            out[i] = edge_of_rank[r]
    return out


def _counts(*cols: list) -> dict:
    d: dict = {}
    for row in zip(*cols):
        d[row] = d.get(row, 0) + 1
    return d


def _entropy_from_counts(counts: dict, n: int) -> float:
    return -sum((c / n) * math.log(c / n) for c in counts.values() if c)


# ---------------------------------------------------------------------------
# Estimators (nats). Raw — never quote one without a null.
# ---------------------------------------------------------------------------

def mi(x: list, y: list, bins: int = 5) -> float:
    """Plug-in mutual information, in nats. Biased upward. See module docstring."""
    n = len(x)
    if n < 2 or n != len(y):
        return 0.0
    bx, by = quantise(x, bins), quantise(y, bins)
    hx = _entropy_from_counts(_counts(bx), n)
    hy = _entropy_from_counts(_counts(by), n)
    hxy = _entropy_from_counts(_counts(bx, by), n)
    return max(0.0, hx + hy - hxy)


def cmi(x: list, y: list, z: list, bins: int = 4) -> float:
    """Conditional mutual information I(X;Y|Z), in nats.

    I(X;Y|Z) = H(X,Z) + H(Y,Z) - H(X,Y,Z) - H(Z). Bins default lower than for
    MI because the grid is B**3 here: at 5 bins and 400 samples the average
    cell holds three observations, and an entropy estimated from three
    observations per cell is mostly bias.
    """
    n = len(x)
    if n < 2 or not (n == len(y) == len(z)):
        return 0.0
    bx, by, bz = quantise(x, bins), quantise(y, bins), quantise(z, bins)
    h_xz = _entropy_from_counts(_counts(bx, bz), n)
    h_yz = _entropy_from_counts(_counts(by, bz), n)
    h_xyz = _entropy_from_counts(_counts(bx, by, bz), n)
    h_z = _entropy_from_counts(_counts(bz), n)
    return max(0.0, h_xz + h_yz - h_xyz - h_z)


def cells_per_sample(n: int, bins: int, dims: int) -> float:
    return n / max(1, bins ** dims)


# ---------------------------------------------------------------------------
# Public results
# ---------------------------------------------------------------------------

@dataclass
class Dependence:
    kind: str
    n: int
    bins: int
    nats: float
    surrogate: dict = field(default_factory=dict)
    stability: list = field(default_factory=list)
    stable: bool = False
    verdict: str = "INSUFFICIENT_EVIDENCE"
    warnings: list = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _stability(runs: list) -> tuple[bool, str]:
    """Is the verdict the same across the bin grid?

    Requiring unanimity rather than a majority. A relationship visible at one
    resolution and absent at the two either side of it is a property of the
    grid, not of the data, and §18 lists exactly that — instability under
    parameter perturbation — as a warning sign.
    """
    if not runs:
        return False, "no bin counts were usable"
    sig = [r["significant"] for r in runs]
    if all(sig):
        return True, f"significant at every bin count tried ({len(runs)})"
    if not any(sig):
        return True, f"not significant at any bin count tried ({len(runs)})"
    hits = [r["bins"] for r in runs if r["significant"]]
    return False, (f"significant only at bins={hits} and not at the others. "
                   f"That is a property of the binning, not of the data")


def _run(estimator, series: tuple, *, bins: int, draws: int, rng: Rng,
         null: str, alpha: float, surrogate_index: int) -> dict:
    """One bin count: observed statistic plus its surrogate distribution."""
    obs = estimator(*series, bins)
    sur = []
    for _ in range(draws):
        cols = list(series)
        target = cols[surrogate_index]
        cols[surrogate_index] = (cyclic_shift(target, rng) if null == "cyclic"
                                 else shuffle(target, rng))
        sur.append(estimator(*cols, bins))
    t = test(obs, sur, null=null, n=len(series[0]), alpha=alpha)
    d = t.to_dict()
    d["bins"] = bins
    return d


def _assess(kind: str, estimator, series: tuple, *, dims: int, draws: int,
            seed: int, null: str, alpha: float, min_n: int,
            surrogate_index: int) -> Dependence:
    n = len(series[0])
    res = Dependence(kind=kind, n=n, bins=0, nats=0.0)

    if n < min_n:
        res.warnings.append(
            f"n={n} is below the {min_n}-sample floor for this estimator. "
            f"Below it the bias term is the same size as anything that could "
            f"be found, so no result is produced at all")
        res.note = "INSUFFICIENT_EVIDENCE — §33"
        return res

    # A near-constant series quantises into one bin, every entropy is 0, and
    # the estimator returns 0 nats — indistinguishable in the output from "we
    # measured this and found no dependence". Real tape produces exactly this:
    # tokens that traded thousands of times at a single price. §41 — the
    # difference between a null result and an impossible measurement is
    # reported, not smoothed over.
    for idx, col in enumerate(series):
        if len(set(col)) < 3:
            res.verdict = "DEGENERATE_SERIES"
            res.warnings.append(
                f"series {idx} takes {len(set(col))} distinct value(s) across "
                f"{n} observations; it cannot carry or receive information")
            res.note = ("not a negative result — there was nothing to "
                        "measure. A constant series has zero entropy, so "
                        "every dependence with it is zero by construction")
            return res

    runs = []
    for bins in BIN_GRID:
        per_cell = cells_per_sample(n, bins, dims)
        if per_cell < MIN_SAMPLES_PER_CELL:
            res.warnings.append(
                f"bins={bins} skipped: {per_cell:.1f} samples per cell, below "
                f"the {MIN_SAMPLES_PER_CELL:g} floor")
            continue
        runs.append(_run(estimator, series, bins=bins, draws=draws,
                         rng=Rng(seed + bins), null=null, alpha=alpha,
                         surrogate_index=surrogate_index))

    res.stability = runs
    if not runs:
        res.note = ("no bin count leaves enough samples per cell. This is a "
                    "sample-size limit, not a negative result")
        return res

    res.stable, stab_note = _stability(runs)
    mid = runs[len(runs) // 2]
    res.bins = mid["bins"]
    res.nats = mid["statistic"]
    res.surrogate = mid

    any_sig = any(r["significant"] for r in runs)
    if any_sig and res.stable:
        res.verdict = "STRUCTURE_PRESENT"
    elif any_sig:
        res.verdict = "BINNING_ARTEFACT"
    else:
        res.verdict = "NO_STRUCTURE_FOUND"

    res.note = (
        f"{stab_note}. Raw {kind} at bins={res.bins} is {res.nats:.5f} nats "
        f"against a null mean of {mid['null_mean']:.5f} — the null mean IS the "
        f"estimator's bias, and only the difference is a finding. "
        + ("A significant result here is evidence of dependence, not of an "
           "exploitable edge: it has paid no fee, crossed no spread and waited "
           "out no latency."
           if res.verdict == "STRUCTURE_PRESENT" else
           "Reported as an artefact because the verdict flips with the bin "
           "count."
           if res.verdict == "BINNING_ARTEFACT" else
           "§33: finding nothing is an answer."))
    return res


# ---------------------------------------------------------------------------
# The three public tests
# ---------------------------------------------------------------------------

def mutual_information(x: list, y: list, *, draws: int = 500, seed: int = 20260825,
                       null: str = "cyclic", alpha: float = 0.05,
                       min_n: int = 120) -> Dependence:
    """I(X;Y) against a surrogate null, across the bin grid.

    `null='cyclic'` for time-ordered series — the default, because calling
    ordinary autocorrelation a discovery is the single easiest way to be wrong
    here. Pass `null='shuffle'` only for genuinely exchangeable observations
    where order carries nothing.
    """
    return _assess("mutual_information", mi, (list(x), list(y)), dims=2,
                   draws=draws, seed=seed, null=null, alpha=alpha,
                   min_n=min_n, surrogate_index=1)


def conditional_mutual_information(x: list, y: list, z: list, *,
                                   draws: int = 500, seed: int = 20260825,
                                   null: str = "cyclic", alpha: float = 0.05,
                                   min_n: int = 300) -> Dependence:
    """I(X;Y|Z) — does X say anything about Y that Z does not already say?

    The floor is higher than for MI because the grid is one dimension deeper.
    """
    return _assess("conditional_mutual_information", cmi,
                   (list(x), list(y), list(z)), dims=3, draws=draws,
                   seed=seed, null=null, alpha=alpha, min_n=min_n,
                   surrogate_index=0)


def transfer_entropy(source: list, target: list, *, lag: int = 1,
                     draws: int = 500, seed: int = 20260825,
                     alpha: float = 0.05, min_n: int = 300) -> Dependence:
    """TE(source -> target) = I(target_{t+lag} ; source_t | target_t).

    Directional, and the direction is only meaningful because of the
    conditioning: without `target_t` in the condition this reduces to a lagged
    mutual information, which is symmetric under swapping the two series and
    therefore says nothing about direction at all.

    A significant TE means source carries information about target's next move
    beyond target's own present. It does NOT mean source causes target — an
    unobserved third series driving both, with a shorter path to source,
    produces exactly this reading. §24: correlation is not causation, and
    neither is conditional mutual information.
    """
    n = min(len(source), len(target))
    if n <= lag + 1:
        return Dependence(kind="transfer_entropy", n=n, bins=0, nats=0.0,
                          note="series shorter than the lag")
    y_future = list(target[lag:n])
    x_now = list(source[:n - lag])
    y_now = list(target[:n - lag])

    res = _assess("transfer_entropy", cmi, (x_now, y_future, y_now), dims=3,
                  draws=draws, seed=seed, null="cyclic", alpha=alpha,
                  min_n=min_n, surrogate_index=0)
    res.note += (f" Direction is source -> target at lag {lag}, conditioned on "
                 f"the target's own present. Information flow, never causation "
                 f"(§24): a hidden common driver closer to the source produces "
                 f"this same reading.")
    return res


def transfer_entropy_both_ways(a: list, b: list, *, lag: int = 1,
                               **kw) -> dict:
    """Both directions, and the asymmetry between them.

    Reported as a pair because a one-directional transfer entropy is almost
    always misread. Net flow is only interpretable when one direction is
    significant and the other is not; when both are significant the honest
    reading is a feedback loop or a common driver, not a leader.
    """
    ab = transfer_entropy(a, b, lag=lag, **kw)
    ba = transfer_entropy(b, a, lag=lag, **kw)
    sig_ab = ab.verdict == "STRUCTURE_PRESENT"
    sig_ba = ba.verdict == "STRUCTURE_PRESENT"
    if sig_ab and not sig_ba:
        reading = "A leads B"
    elif sig_ba and not sig_ab:
        reading = "B leads A"
    elif sig_ab and sig_ba:
        reading = ("bidirectional — feedback, or a common driver. No leader "
                   "may be named from this")
    else:
        reading = "no information flow found in either direction"
    return {"a_to_b": ab.to_dict(), "b_to_a": ba.to_dict(),
            "net_nats": round(ab.nats - ba.nats, 6), "reading": reading,
            "note": ("net flow is only interpretable when exactly one "
                     "direction survives its surrogates. Subtracting two "
                     "noisy, biased estimates does not create a direction")}


# ---------------------------------------------------------------------------
# Lead-lag
# ---------------------------------------------------------------------------

@dataclass
class LeadLag:
    n: int
    best_lag: int = 0
    best_corr: float = 0.0
    surrogate: dict = field(default_factory=dict)
    profile: list = field(default_factory=list)
    verdict: str = "NO_LEAD_LAG_FOUND"
    note: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _pearson(a: list, b: list) -> float:
    n = len(a)
    if n < 3:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return 0.0
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / math.sqrt(va * vb)


def _max_abs_xcorr(a: list, b: list, max_lag: int) -> tuple[int, float, list]:
    n = min(len(a), len(b))
    prof = []
    best_lag, best = 0, 0.0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            x, y = a[:n - lag], b[lag:n]
        else:
            x, y = a[-lag:n], b[:n + lag]
        if len(x) < 20:
            continue
        r = _pearson(list(x), list(y))
        prof.append({"lag": lag, "corr": round(r, 5), "pairs": len(x)})
        if abs(r) > abs(best):
            best_lag, best = lag, r
    return best_lag, best, prof


def lead_lag(a: list, b: list, *, max_lag: int = 20, draws: int = 500,
             seed: int = 20260825, alpha: float = 0.05) -> LeadLag:
    """Does A move before B, or B before A?

    The trap this guards is the multiplicity one. Scanning 41 lags and
    reporting the best correlation gives a spuriously strong reading every
    time; a t-test at that lag is meaningless because the lag was chosen by
    looking. So the surrogate null is drawn on the SAME statistic — the
    maximum absolute cross-correlation over the whole lag window — which
    prices the search in automatically, and cyclic-shift surrogates keep both
    series' own autocorrelation intact so that two independent random walks do
    not read as a lead-lag relationship.
    """
    n = min(len(a), len(b))
    res = LeadLag(n=n)
    if n < 60:
        res.note = (f"n={n} is too short: with {2 * max_lag + 1} lags searched "
                    f"the best of them is noise at this length")
        return res

    a, b = list(a[:n]), list(b[:n])
    best_lag, best, prof = _max_abs_xcorr(a, b, max_lag)
    rng = Rng(seed)
    sur = [abs(_max_abs_xcorr(a, cyclic_shift(b, rng), max_lag)[1])
           for _ in range(draws)]
    t = test(abs(best), sur, null="cyclic", n=n, alpha=alpha)

    res.best_lag, res.best_corr = best_lag, round(best, 5)
    res.surrogate, res.profile = t.to_dict(), prof
    if t.significant and best_lag > 0:
        res.verdict = "A_LEADS_B"
    elif t.significant and best_lag < 0:
        res.verdict = "B_LEADS_A"
    elif t.significant:
        res.verdict = "CONTEMPORANEOUS"
    res.note = (
        f"best |r| = {abs(best):.4f} at lag {best_lag}, against a null whose "
        f"mean best |r| over the same {2 * max_lag + 1}-lag search is "
        f"{t.null_mean:.4f}. The null is that large because searching 41 lags "
        f"finds a good one in pure noise; comparing against zero instead "
        f"would have made this look significant regardless of the data. "
        + ("A lead-lag reading is an analysis result, not a strategy: the "
           "observation matrix is one row per wallet-trade, so no market-pair "
           "hypothesis can enter `pqv3 discover` without a different matrix "
           "build." if res.verdict != "NO_LEAD_LAG_FOUND" else "§33."))
    return res
