"""Data-quality rules for what research is allowed to study and be charged.

Integrated from the external audit: study only the stretch of a market where
the outcome was still in doubt, charge the real (or assumed) spread rather
than zero, scale exits to what they must clear, size positions from the
account, and hold for wall-clock time rather than bar counts.
"""

from __future__ import annotations

from pqb.research import (effective_spread, exit_ladders, hold_ladder,
                          position_ladder, uncertain_span)


def _rows(prices):
    return [{"ts": 1_000 + i, "price": p} for i, p in enumerate(prices)]


# -- the uncertainty band ------------------------------------------------------

def test_settled_tail_is_trimmed():
    series = _rows([0.5] * 100 + [0.995] * 300)
    kept = uncertain_span(series, 0.10, 0.90)
    assert len(kept) == 100
    assert all(0.10 <= r["price"] <= 0.90 for r in kept)


def test_longest_contiguous_run_wins_no_splicing():
    """Dropping mid-series rows would invent price moves across the join, so
    the longest single in-band stretch is taken instead."""
    series = _rows([0.5] * 50 + [0.99] * 20 + [0.5] * 200 + [0.995] * 30)
    kept = uncertain_span(series, 0.10, 0.90)
    assert len(kept) == 200
    stamps = [r["ts"] for r in kept]
    assert stamps == sorted(stamps)
    assert stamps[-1] - stamps[0] == 199        # contiguous, no gap spliced over


def test_fully_decided_series_trims_to_nothing():
    assert uncertain_span(_rows([0.999] * 500), 0.10, 0.90) == []


def test_fully_in_play_series_is_untouched():
    series = _rows([0.4, 0.5, 0.6] * 100)
    assert uncertain_span(series, 0.10, 0.90) == series


# -- realistic costs -----------------------------------------------------------

def test_unmeasurable_spread_is_assumed_not_waived():
    spread, measured = effective_spread(0.0, 0.010)
    assert spread == 0.010 and measured is False
    spread, measured = effective_spread(0.02, 0.010)
    assert spread == 0.02 and measured is True


def test_every_target_clears_the_round_trip():
    """A target below one round trip is not ambitious, it is a guaranteed
    loss — the exact artifact the audit caught (two-tick targets against a
    ten-tick spread)."""
    stops, targets = exit_ladders(spread=0.010, price=0.51)
    round_trip_pct = 0.010 / 0.51 * 100.0
    assert all(t > round_trip_pct for t in targets)
    assert all(s >= round_trip_pct for s in stops)


def test_position_ladder_sizes_from_the_account_not_one_share():
    """One-share positions on a $10,000 account made every equity curve flat
    by construction."""
    ladder, top = position_ladder(price=0.50, equity=10_000.0, fraction=0.25)
    assert top == 5_000                      # 25% of $10k at $0.50/share
    assert ladder[-1] == top
    assert len(ladder) >= 3                  # real choices, not a single count


def test_hold_ladder_is_wall_clock_not_bar_count():
    """Doubling series resolution must not silently halve every holding
    period: the same wall-clock spans convert to twice the bars."""
    coarse = hold_ladder(bar_seconds=600.0)
    fine = hold_ladder(bar_seconds=300.0)
    assert coarse[0] == fine[0] == 0         # 0 = no time exit, always offered
    # 30 minutes at 10-minute bars = 3 bars; at 5-minute bars = 6 bars.
    assert fine[1] == coarse[1] * 2
    assert hold_ladder(0.0) == [0, 10, 25, 50, 100]   # unknown cadence: as-is
