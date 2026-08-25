"""The matched baseline. The control that decides whether anything here is real.

**Why a raw baseline is not good enough.** Returns are measured as
`(resolution - p) / p`. That denominator makes a winning longshot enormous: a
token bought at 0.05 that resolves YES returns +19.0, while a 0.90 favourite
returns +0.11. So the mean return over ALL observations is dominated by a few
longshot wins and is strongly negative overall on this tape.

Against that baseline, ANY rule that merely avoids longshots looks spectacular.
The first screening run of this system produced exactly that: `price >= 0.53`
scored +0.50 "alpha", which is not an edge at all — it is a price preference,
and it is available to anyone who types a number.

**The fix.** Compare every observation only against other observations that
share its price band and its week. What survives is the part attributable to
the rule rather than to the band it happens to select or the week it happens to
trade.

The comparison is leave-one-out: an observation is never part of its own
baseline. Without that, a rule admitting most of a bucket competes largely
against itself and its measured excess is pulled toward zero in proportion to
how much of the bucket it occupies.

Buckets with fewer than `MIN_BUCKET` members are dropped rather than compared,
because a baseline drawn from three observations is noise, and comparing
against noise manufactures alpha in whichever direction the noise happened to
fall.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from .matrix import Matrix

# Price bands. Identical to `intelligence/wallets.py` so a wallet's alpha and a
# rule's alpha are measured against the same partition and can be compared.
BANDS = ((0.02, 0.20), (0.20, 0.35), (0.35, 0.50),
         (0.50, 0.65), (0.65, 0.80), (0.80, 0.98))

WEEK = 7 * 86_400
MIN_BUCKET = 10


def band_index(price: float) -> int:
    for k, (lo, hi) in enumerate(BANDS):
        if lo <= price < hi:
            return k
    return len(BANDS) - 1


@dataclass
class MatchedBaseline:
    """Per-row returns and the (band, week) pools they are judged against."""

    ret: dict = field(default_factory=dict)        # row -> raw return
    bucket: dict = field(default_factory=dict)     # row -> bucket key
    sums: dict = field(default_factory=dict)       # bucket -> sum of returns
    counts: dict = field(default_factory=dict)     # bucket -> n
    lo: int = 0
    hi: int = 0
    raw_mean: float = 0.0

    def excess(self, i: float) -> float | None:
        """Leave-one-out excess for one row, or None if not comparable."""
        b = self.bucket.get(i)
        if b is None:
            return None
        n = self.counts[b]
        if n < MIN_BUCKET:
            return None
        r = self.ret[i]
        return r - (self.sums[b] - r) / (n - 1)

    def excess_series(self, rows) -> list:
        out = []
        for i in rows:
            e = self.excess(i)
            if e is not None:
                out.append(e)
        return out

    def describe(self) -> dict:
        usable = sum(n for n in self.counts.values() if n >= MIN_BUCKET)
        return {"rows": len(self.ret), "buckets": len(self.counts),
                "comparable_rows": usable,
                "raw_mean_return": round(self.raw_mean, 6),
                "min_bucket": MIN_BUCKET,
                "note": ("returns are compared only within the same price band "
                         "and week, leave-one-out. A rule that merely selects "
                         "favourites scores zero here, which is the point")}


def build(m: Matrix, st: Settings, lo: int, hi: int,
          rows=None) -> MatchedBaseline:
    """Pools built from EVERY observation in the window.

    Not only the rows a rule admits — the baseline must describe what was
    available to anyone trading that band in that week, including the trades
    the rule declined.
    """
    mb = MatchedBaseline(lo=lo, hi=hi)
    cost = 1.0 + (st.costs.slippage_bps + st.costs.fee_bps) / 10_000.0
    price = m.cols["price"]
    total = 0.0
    it = range(lo, hi) if rows is None else rows
    for i in it:
        p = price[i] * cost
        if not (0 < p < 1):
            continue
        r = (m.resolution[i] - p) / p
        b = (band_index(m.cols["price"][i]), m.ts[i] // WEEK)
        mb.ret[i] = r
        mb.bucket[i] = b
        mb.sums[b] = mb.sums.get(b, 0.0) + r
        mb.counts[b] = mb.counts.get(b, 0) + 1
        total += r
    if mb.ret:
        mb.raw_mean = total / len(mb.ret)
    return mb
