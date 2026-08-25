"""Python reference implementations of the Rust kernels.

These are AUTHORITATIVE. The Rust crate must agree with them exactly for
integer results and within tolerance for floats, and until that is proven
against golden data the accelerator runs in shadow mode and returns these.

Each is a pure function of its arguments — no I/O, no clock, no global state —
which is what makes mechanical equivalence testing possible at all. A kernel
that read the database could not be compared, only trusted.
"""

from __future__ import annotations

import statistics


def alpha_excess(returns: list, bucket_ids: list, bucket_sums: list,
                 bucket_counts: list, min_n: int = 10) -> tuple:
    """Leave-one-out excess return against a bucket baseline.

    The wallet-alpha control. See `intelligence/wallets.py` for why subtracting
    the trade's own contribution from its baseline is not optional.
    """
    if len(returns) != len(bucket_ids):
        raise ValueError("returns and bucket_ids must be the same length")
    total = 0.0
    n = 0
    for r, b in zip(returns, bucket_ids):
        if b >= len(bucket_counts):
            continue
        count = bucket_counts[b]
        if count < min_n or count < 2:
            continue
        total += r - (bucket_sums[b] - r) / (count - 1)
        n += 1
    return (0.0, 0) if n == 0 else (total / n, n)


def block_bootstrap(values: list, draws: int, block_size: int,
                    seed: int) -> list:
    """Moving-block bootstrap of the mean.

    Blocks, not individual draws: prediction-market returns are serially
    dependent because one market resolves many positions at once, and an
    i.i.d. bootstrap over dependent data reports an interval far too narrow.

    The generator is xorshift64* rather than `random` so that the Rust kernel
    can reproduce the identical sequence and equivalence can be asserted on the
    values rather than only on their distribution.
    """
    n = len(values)
    if n == 0 or draws <= 0:
        return []
    block = max(1, min(block_size, n))
    n_blocks = (n + block - 1) // block
    state = seed if seed else 0x9E3779B97F4A7C15
    mask = (1 << 64) - 1
    out = []
    for _ in range(draws):
        total = 0.0
        taken = 0
        for _ in range(n_blocks):
            state ^= (state >> 12)
            state = (state ^ (state << 25)) & mask
            state ^= (state >> 27)
            start = (state * 0x2545F4914F6CDD1D & mask) % n
            for k in range(block):
                if taken >= n:
                    break
                total += values[(start + k) % n]
                taken += 1
        out.append(0.0 if taken == 0 else total / taken)
    return out


def max_drawdown(equity: list) -> float:
    """Maximum peak-to-trough drawdown, as a fraction of peak.

    Peaks at or below zero are skipped rather than divided by — a curve that
    crosses zero has no meaningful fractional drawdown, and an infinity here
    would propagate silently into a risk limit.
    """
    peak = float("-inf")
    worst = 0.0
    for e in equity:
        if e > peak:
            peak = e
        if peak > 0.0:
            dd = (peak - e) / peak
            if dd > worst:
                worst = dd
    return worst


def transition_chi2(symbols: list) -> tuple:
    """Chi-square for independence in a two-state sequence.

    Returns (chi2, n_transitions, p_up). Critical value at df=1, alpha=0.05 is
    3.84. Below it, the sequence is indistinguishable from independent and
    Agent 9 abstains rather than reporting weak structure.
    """
    s = [x for x in symbols if x in (0, 1)]
    if len(s) < 3:
        return (0.0, 0, 0.0)
    trans = [[0, 0], [0, 0]]
    for a, b in zip(s, s[1:]):
        trans[a][b] += 1
    p_up = sum(s) / len(s)
    n = sum(sum(row) for row in trans)
    chi = 0.0
    for a in (0, 1):
        row = sum(trans[a])
        if row == 0:
            continue
        for b in (0, 1):
            p = p_up if b == 1 else 1.0 - p_up
            expected = row * p
            if expected > 0:
                d = trans[a][b] - expected
                chi += d * d / expected
    return (chi, n, p_up)


KERNELS = {
    "alpha_excess": alpha_excess,
    "block_bootstrap": block_bootstrap,
    "max_drawdown": max_drawdown,
    "transition_chi2": transition_chi2,
}
