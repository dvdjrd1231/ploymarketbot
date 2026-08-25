"""The Python reference implementations of every accelerated kernel.

Python is the reference. Rust must match THESE functions, not the other way
round -- so when the two disagree, the Rust is wrong by definition and shadow
mode's report is actionable rather than a debate.

The flattening functions here define the wire format. `rust/src/lib.rs`
documents the same field order, and `tests/test_accel_equivalence.py` asserts
the widths agree, because a silent column shift would compare the right numbers
in the wrong places and produce plausible nonsense.
"""

from __future__ import annotations

import math

# Field order for one observation. MUST match `Obs` in rust/src/lib.rs.
OBS_FIELDS = (
    "price", "notional", "rel_notional", "w_settled_n", "w_win_rate",
    "w_roll_win_rate", "w_roll_roi", "w_edge_t", "w_consec_losses",
    "w_token_repeat", "market_recent_prints", "market_price_move",
    "secs_to_settle", "resolution",
)
OBS_WIDTH = len(OBS_FIELDS)

# Field order for one candidate's filters. MUST match `Filters` in lib.rs.
FILTER_FIELDS = (
    "min_price", "max_price", "min_notional", "max_notional",
    "min_rel_notional", "min_settled_n", "min_win_rate", "min_roll_win_rate",
    "min_roll_roi", "min_edge_t", "max_consec_losses", "skip_repeat_token",
    "min_market_prints", "max_market_move", "min_market_move",
    "max_secs_to_settle", "min_secs_to_settle",
)
FILTER_WIDTH = len(FILTER_FIELDS)

NAN = float("nan")


def flatten_observations(observations) -> list:
    """Observations -> flat float array, in OBS_FIELDS order."""
    out = []
    for o in observations:
        out.extend((
            float(o.price), float(o.notional), float(o.rel_notional),
            float(o.w_settled_n), float(o.w_win_rate),
            float(o.w_roll_win_rate), float(o.w_roll_roi), float(o.w_edge_t),
            float(o.w_consec_losses), 1.0 if o.w_token_repeat else 0.0,
            float(o.market_recent_prints), float(o.market_price_move),
            float(o.secs_to_settle), float(o.trade.resolution),
        ))
    return out


def flatten_filters(strategies) -> list:
    """Strategies -> flat float array. `None` becomes NaN, meaning
    'no constraint' -- the same convention `unset()` reads in Rust."""
    out = []
    for s in strategies:
        for name in FILTER_FIELDS:
            if name == "skip_repeat_token":
                out.append(1.0 if s.skip_repeat_token else 0.0)
                continue
            v = getattr(s, name, None)
            out.append(NAN if v is None else float(v))
    return out


def _unset(v: float) -> bool:
    return v != v          # NaN is the only value not equal to itself


def _admits(o, f) -> bool:
    """Mirror of CopyStrategy.admits_fast over the flat representation."""
    (price, notional, rel_notional, settled_n, win_rate, roll_win_rate,
     roll_roi, edge_t, consec_losses, token_repeat, market_prints,
     market_move, secs_to_settle, _res) = o
    (min_price, max_price, min_notional, max_notional, min_rel_notional,
     min_settled_n, min_win_rate, min_roll_win_rate, min_roll_roi, min_edge_t,
     max_consec_losses, skip_repeat, min_market_prints, max_market_move,
     min_market_move, max_secs, min_secs) = f

    if not _unset(min_price) and price < min_price:
        return False
    if not _unset(max_price) and price > max_price:
        return False
    if not _unset(min_notional) and notional < min_notional:
        return False
    if not _unset(max_notional) and notional > max_notional:
        return False
    if not _unset(min_rel_notional) and rel_notional < min_rel_notional:
        return False
    if not _unset(min_settled_n) and settled_n < min_settled_n:
        return False
    if not _unset(min_win_rate) and win_rate < min_win_rate:
        return False
    if not _unset(min_roll_win_rate) and roll_win_rate < min_roll_win_rate:
        return False
    if not _unset(min_roll_roi) and roll_roi < min_roll_roi:
        return False
    if not _unset(min_edge_t) and edge_t < min_edge_t:
        return False
    if not _unset(max_consec_losses) and consec_losses > max_consec_losses:
        return False
    if skip_repeat > 0.5 and token_repeat > 0.5:
        return False
    if not _unset(min_market_prints) and market_prints < min_market_prints:
        return False
    if not _unset(max_market_move) and abs(market_move) > max_market_move:
        return False
    if not _unset(min_market_move) and abs(market_move) < min_market_move:
        return False
    if not _unset(max_secs) and (secs_to_settle < 0 or secs_to_settle > max_secs):
        return False
    if not _unset(min_secs) and (secs_to_settle < 0 or secs_to_settle < min_secs):
        return False
    return True


def sweep_admit(obs: list, filters: list, cost_mult: float,
                min_price: float, max_price: float) -> list:
    """Reference implementation of the sweep kernel.

    Returns [n_admitted, n_wins, sum_return, sum_sq_return] per candidate.
    """
    if len(obs) % OBS_WIDTH:
        raise ValueError(f"obs length {len(obs)} not a multiple of {OBS_WIDTH}")
    if len(filters) % FILTER_WIDTH:
        raise ValueError(
            f"filters length {len(filters)} not a multiple of {FILTER_WIDTH}")

    rows = [obs[i:i + OBS_WIDTH] for i in range(0, len(obs), OBS_WIDTH)]
    out = []
    for c in range(0, len(filters), FILTER_WIDTH):
        f = filters[c:c + FILTER_WIDTH]
        n_admitted = n_wins = 0.0
        sum_r = sum_r2 = 0.0
        for row in rows:
            if not _admits(row, f):
                continue
            entry = row[0] * cost_mult
            if not (min_price < entry < max_price):
                continue
            r = (row[13] - entry) / entry
            n_admitted += 1.0
            if r > 0.0:
                n_wins += 1.0
            sum_r += r
            sum_r2 += r * r
        out.extend((n_admitted, n_wins, sum_r, sum_r2))
    return out


def t_stat(returns: list) -> tuple:
    """Two-pass mean / sd / t. Two-pass on purpose: the one-pass sum-of-squares
    shortcut loses precision badly when the mean is large relative to the
    variance, which is the normal case here (a 0.05 entry resolving YES returns
    +19.0)."""
    n = len(returns)
    if n < 5:
        return 0.0, 0.0, 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var <= 0:
        return mean, 0.0, 0.0
    sd = math.sqrt(var)
    return mean, sd, mean / (sd / math.sqrt(n))


def bootstrap_means(returns: list, draws: int, seed: int) -> list:
    """Bootstrap using the SAME LCG as the Rust kernel.

    Deliberately not `random.Random`: an equivalence test between two different
    generators can never pass, and a shadow mode that always reports divergence
    is a shadow mode nobody reads.
    """
    n = len(returns)
    if n == 0:
        return []
    state = (seed * 6364136223846793005 + 1) & 0xFFFFFFFFFFFFFFFF
    out = []
    for _ in range(draws):
        acc = 0.0
        for _ in range(n):
            state = (state * 6364136223846793005 + 1442695040888963407) \
                & 0xFFFFFFFFFFFFFFFF
            acc += returns[(state >> 33) % n]
        out.append(acc / n)
    out.sort()
    return out


def max_drawdown(equity: list) -> float:
    peak = worst = 0.0
    for e in equity:
        peak = max(peak, e)
        worst = min(worst, e - peak)
    return abs(worst)


KERNELS = {
    "sweep_admit": sweep_admit,
    "t_stat": t_stat,
    "bootstrap_means": bootstrap_means,
    "max_drawdown": max_drawdown,
}


def rust_kernel(name: str):
    """The Rust counterpart, or None if the extension is not built."""
    from . import _try_import
    backend = _try_import()
    return getattr(backend, name, None) if backend is not None else None
