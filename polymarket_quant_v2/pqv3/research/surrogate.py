"""Surrogate data. The null hypothesis, constructed rather than assumed.

Every estimator added in this batch — mutual information, transfer entropy,
Lomb-Scargle power, HMM likelihood, cross-correlation — shares one property
that makes analytic p-values useless: the statistic is BIASED AWAY FROM ZERO
under the null. Estimated mutual information between two independent series is
not 0, it is roughly (Bx-1)(By-1)/2N. The largest periodogram peak of pure
noise is not small, it grows with the number of frequencies searched. A 3-state
HMM fitted to i.i.d. data will always find three states and will always beat a
1-state model on likelihood.

So a raw value of any of them is uninterpretable, and quoting one is the
"confidence without evidence" §43 closes on. What IS interpretable is the value
compared against the same estimator run on data that has been stripped of the
structure being tested for and nothing else. That is what this module builds.

Three nulls, and choosing between them is the whole design decision:

    `shuffle`       destroys everything: order, autocorrelation, dependence.
                    The right null for "are X and Y related at all", the WRONG
                    null for anything in a time series, because it will call
                    ordinary autocorrelation a discovery.

    `cyclic_shift`  preserves each series' own autocorrelation and marginal
                    distribution exactly, destroys only the alignment between
                    them. The right null for lead-lag, transfer entropy and
                    cross-market structure: it asks "is this relationship more
                    than what two independently autocorrelated series produce
                    by coincidence", which is the question actually being
                    asked.

    `block`         preserves short-range structure within one series while
                    destroying long-range structure. The right null for
                    periodicity and for path-dependent statistics.

`Rng` is a seeded xorshift rather than `random`, so a result is reproducible
from `research.seed` alone and cannot silently change because something else in
the process drew from the global generator first.
"""

from __future__ import annotations

from dataclasses import dataclass


class Rng:
    """xorshift64*. Deterministic, seeded, independent of global state."""

    __slots__ = ("_s",)

    def __init__(self, seed: int) -> None:
        # 0 is a fixed point of xorshift; any nonzero seed is fine.
        self._s = (seed or 0x2545F4914F6CDD1D) & 0xFFFFFFFFFFFFFFFF

    def _next(self) -> int:
        s = self._s
        s ^= (s << 13) & 0xFFFFFFFFFFFFFFFF
        s ^= s >> 7
        s ^= (s << 17) & 0xFFFFFFFFFFFFFFFF
        self._s = s
        return (s * 0x2545F4914F6CDD1D) & 0xFFFFFFFFFFFFFFFF

    def random(self) -> float:
        return (self._next() >> 11) / 9007199254740992.0     # 53-bit mantissa

    def randrange(self, n: int) -> int:
        if n <= 0:
            return 0
        # Rejection sampling: `% n` biases the low residues, and over 2000
        # draws that bias is measurable in the tail of a p-value.
        limit = (1 << 64) - ((1 << 64) % n)
        while True:
            v = self._next()
            if v < limit:
                return v % n

    def shuffled(self, xs: list) -> list:
        out = list(xs)
        for i in range(len(out) - 1, 0, -1):
            j = self.randrange(i + 1)
            out[i], out[j] = out[j], out[i]
        return out


def shuffle(xs: list, rng: Rng) -> list:
    """Destroy all order. Null: X and Y are unrelated AND memoryless."""
    return rng.shuffled(xs)


def cyclic_shift(xs: list, rng: Rng, *, min_shift: int = 1) -> list:
    """Rotate by a random offset.

    Preserves the series' own autocorrelation function and its marginal
    distribution exactly — the rotated series IS the original series, read
    from a different starting point. Only the alignment with a second series
    is destroyed, which is precisely the thing under test.

    The seam introduced at the wrap point is one discontinuity in n samples.
    It is the known cost of this surrogate and it biases towards the null
    (slightly less structure), which is the safe direction.
    """
    n = len(xs)
    if n < 3:
        return list(xs)
    hi = n - min_shift * 2
    k = min_shift + (rng.randrange(hi) if hi > 0 else 0)
    return xs[k:] + xs[:k]


def block(xs: list, rng: Rng, *, block_len: int = 0) -> list:
    """Resample contiguous blocks with replacement.

    Short-range structure survives inside a block, long-range structure does
    not. `block_len` defaults to n**(1/3), the standard rule for a stationary
    bootstrap, rounded to at least 2 — a block length of 1 is just `shuffle`
    wearing a different name.
    """
    n = len(xs)
    if n < 4:
        return list(xs)
    L = block_len or max(2, int(round(n ** (1 / 3))))
    out: list = []
    while len(out) < n:
        start = rng.randrange(max(1, n - L + 1))
        out.extend(xs[start:start + L])
    return out[:n]


@dataclass
class SurrogateTest:
    """A statistic, its null distribution, and what that combination licenses."""

    statistic: float
    null_mean: float
    null_sd: float
    excess: float
    p_value: float
    draws: int
    null: str
    n: int
    significant: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def test(observed: float, surrogates: list, *, null: str, n: int,
         alpha: float = 0.05, greater_is_structure: bool = True
         ) -> SurrogateTest:
    """Rank the observed statistic within its own null distribution.

    The p-value is `(1 + #{surrogate >= observed}) / (1 + draws)`. The `1 +`
    on both sides is not a rounding nicety: without it the smallest reportable
    p-value is 0, and a p-value of exactly zero from a finite number of draws
    is a statement the experiment cannot support. With it, 2000 draws floor at
    1/2001, which is the honest resolution of the test that was actually run.
    """
    draws = len(surrogates)
    if draws == 0:
        return SurrogateTest(observed, 0.0, 0.0, 0.0, 1.0, 0, null, n,
                             note="no surrogates drawn; nothing is claimed")

    # The p-value cannot go below 1/(draws+1). Testing at alpha=0.05 with 15
    # surrogates makes 0.0625 the smallest attainable value, so the test can
    # NEVER reject however strong the effect — it returns "not significant"
    # for everything and reads exactly like a negative result. That is a
    # silently broken experiment, so it is refused rather than reported.
    floor = 1.0 / (draws + 1)
    if floor > alpha:
        return SurrogateTest(
            round(observed, 6), 0.0, 0.0, 0.0, 1.0, draws, null, n,
            note=(f"UNDERPOWERED: {draws} surrogates floor the p-value at "
                  f"{floor:.4f}, above alpha={alpha}. This test could not "
                  f"reject for any effect size. Use at least "
                  f"{int(round(1 / alpha)) - 1} surrogates"))
    m = sum(surrogates) / draws
    var = sum((s - m) ** 2 for s in surrogates) / draws
    sd = var ** 0.5
    if greater_is_structure:
        beat = sum(1 for s in surrogates if s >= observed)
    else:
        beat = sum(1 for s in surrogates if s <= observed)
    p = (1 + beat) / (1 + draws)
    t = SurrogateTest(statistic=round(observed, 6), null_mean=round(m, 6),
                      null_sd=round(sd, 6),
                      excess=round(observed - m, 6), p_value=round(p, 6),
                      draws=draws, null=null, n=n)
    t.significant = p <= alpha
    t.note = (
        f"the estimator's own bias is inside `null_mean` ({m:.4g}), which is "
        f"what the raw statistic would have been on structureless data of the "
        f"same shape. `excess` is the only part that means anything"
        if not t.significant else
        f"survives {draws} {null} surrogates at alpha={alpha}. That is "
        f"evidence of structure, not of tradability: nothing here has paid a "
        f"fee, crossed a spread or waited out a latency")
    return t
