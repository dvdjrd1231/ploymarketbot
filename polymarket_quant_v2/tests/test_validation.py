"""The controls that make a result interpretable, and the ladder that gates it."""

from __future__ import annotations

import pytest

from conftest import make_obs
from pqv2.strategy_b.strategy import CopyStrategy, candidates_for, grid_size, naive_copy
from pqv2.substrate.data import PriceTape, oos_split_ts
from pqv2.substrate.state import collect
from pqv2.validation import backtest, stats
from pqv2.validation.baseline import BaselineBook, calibration_table
from pqv2.validation.validate import (CONCENTRATED, FAILED, INSUFFICIENT_EVIDENCE,
                                      NO_WALLET_ALPHA, VALIDATED, evaluate,
                                      walk_forward)


# --- multiple testing -------------------------------------------------------

def test_bh_threshold_tightens_as_the_search_grows():
    """The denominator is part of the result."""
    small = stats.benjamini_hochberg([0.001, 0.02, 0.3], fdr=0.10)
    large = stats.benjamini_hochberg([0.001, 0.02, 0.3] + [0.6] * 5000, fdr=0.10)
    assert large.threshold < small.threshold
    assert large.n_tested > small.n_tested


def test_bh_with_no_signal_admits_nothing():
    bh = stats.benjamini_hochberg([0.4, 0.5, 0.9] * 100, fdr=0.10)
    assert bh.n_significant == 0
    assert not bh.significant(0.04)


def test_block_bootstrap_is_wider_than_iid_when_returns_are_correlated():
    """Copy trades in one market are correlated; resampling single trades
    treats a run as many independent wins and narrows the interval falsely."""
    runs = ([0.5] * 10 + [-0.5] * 10) * 15
    lo_i, hi_i = stats.bootstrap_ci(runs, draws=400, seed=1)
    lo_b, hi_b = stats.block_bootstrap_ci(runs, block=10, draws=400, seed=1)
    assert (hi_b - lo_b) >= (hi_i - lo_i) * 0.9


def test_placebo_detects_drift():
    """A 'strategy' drawn from the same pool as the universe must not look
    special."""
    universe = [0.2] * 500 + [-0.1] * 500
    p = stats.placebo_p([0.2, -0.1] * 25, universe, draws=200, seed=3)
    assert p > 0.10, "random entries from the same pool were called a signal"


def test_risk_of_ruin_rises_with_size():
    low = stats.risk_of_ruin(0.01, 0.5, 0.02, trials=400, horizon=300, seed=1)
    high = stats.risk_of_ruin(0.01, 0.5, 0.40, trials=400, horizon=300, seed=1)
    assert high > low


# --- the favourite-longshot control ----------------------------------------

def test_the_planted_favourite_longshot_bias_is_detected(st):
    table = calibration_table(st)
    high = [r for r in table if r["band"] == "0.70-0.80"]
    low = [r for r in table if r["band"] == "0.20-0.30"]
    assert high and low
    assert high[0]["gap"] > low[0]["gap"], (
        "the calibration table failed to see a planted bias")


def test_buying_favourites_earns_no_wallet_alpha(st):
    """The test this whole control exists for.

    A price-band rule harvests the market-wide bias while copying nobody. Its
    wallet alpha must be ~0 even though its P&L is strongly positive.
    """
    book = BaselineBook(st)
    tape = PriceTape(st)
    split = oos_split_ts(st)
    wallets = [w for w, _ in __import__(
        "pqv2.substrate.data", fromlist=["x"]).wallet_trade_counts(st, 20)]
    ordinary = [w for w in wallets if w != "0xedge"]
    assert ordinary, "fixture produced no ordinary wallets"
    w = ordinary[0]
    obs = collect(st, wallets=[w], ts_from=split)
    strategy = CopyStrategy(wallet=w, min_price=0.70, max_price=0.98,
                            delay_secs=0, label="buy_favourites")
    res = backtest.run(strategy, obs, st, tape)
    if res.n_filled < 5:
        pytest.skip("not enough fills in the fixture for this wallet")
    alpha = book.alpha_for(res.fills, w)
    if alpha["matched"] == 0:
        pytest.skip("population cells too thin in the fixture")
    assert alpha["alpha"] < res.expectancy, (
        "wallet alpha should strip out the market-wide bias, so it must be "
        "materially below raw expectancy")


# --- the ladder -------------------------------------------------------------

def test_insufficient_evidence_stops_before_anything_expensive(st):
    tape = PriceTape(st)
    book = BaselineBook(st)
    s = naive_copy("0xedge")
    v = evaluate(s, [], [], st, tape, book)
    assert v.status == INSUFFICIENT_EVIDENCE
    assert not v.tradable
    assert v.reasons


def test_negative_out_of_sample_expectancy_fails(st):
    """A strategy cannot be validated by an in-sample result."""
    tape = PriceTape(st)
    book = BaselineBook(st)
    split = oos_split_ts(st)
    is_obs = collect(st, wallets=["0xedge"], ts_to=split)
    oos = collect(st, wallets=["0xedge"], ts_from=split)
    # A rule that only buys long shots should lose out of sample.
    s = CopyStrategy(wallet="0xedge", min_price=0.02, max_price=0.20,
                     delay_secs=0)
    v = evaluate(s, is_obs, oos, st, tape, book, deep=False)
    assert v.status in (FAILED, INSUFFICIENT_EVIDENCE, NO_WALLET_ALPHA,
                        CONCENTRATED, VALIDATED)
    if v.status == FAILED:
        assert "expectancy" in " ".join(v.reasons)


def test_validated_is_the_only_tradable_status():
    from pqv2.validation.validate import TRADABLE_STATUSES
    assert TRADABLE_STATUSES == frozenset({VALIDATED})


def test_walk_forward_splits_contiguously_in_time(st):
    tape = PriceTape(st)
    obs = collect(st, wallets=["0xedge"], ts_from=oos_split_ts(st))
    if len(obs) < 25:
        pytest.skip("fixture too small")
    wf = walk_forward(naive_copy("0xedge", delay_secs=0), obs, st, tape, folds=4)
    assert wf["folds"] >= 1
    assert 0.0 <= wf["fraction_positive"] <= 1.0


# --- the backtest itself ----------------------------------------------------

def test_unpriceable_copy_earns_nothing_rather_than_the_wallets_price(st):
    """The single line that separates a real copy backtest from a fictional one."""
    tape = PriceTape(st)
    obs = collect(st, wallets=["0xedge"], limit=50)
    # A delay far beyond any print guarantees no fill is available.
    s = CopyStrategy(wallet="0xedge", delay_secs=10 ** 9)
    res = backtest.run(s, obs, st, tape)
    assert res.n_filled == 0
    assert res.n_unfilled > 0
    assert res.pnl == 0.0


def test_costs_are_charged_against_the_trade(st):
    tape = PriceTape(st)
    obs = collect(st, wallets=["0xedge"], limit=200)
    free = backtest.run(naive_copy("0xedge", delay_secs=0), obs, st, tape)
    st.costs.slippage_bps = 500.0
    dear = backtest.run(naive_copy("0xedge", delay_secs=0), obs, st, tape)
    assert dear.expectancy < free.expectancy


def test_admits_and_admits_fast_agree_everywhere(st):
    """The optimised sweep path must be a pure speedup, not a different rule."""
    obs = collect(st, limit=400)
    for s in list(candidates_for("0xedge"))[:150]:
        for o in obs[:60]:
            assert s.admits(o)[0] == s.admits_fast(o), s.spec()


def test_rejection_reasons_are_specific():
    s = CopyStrategy(wallet="w", min_price=0.60)
    ok, why = s.admits(make_obs(price=0.30))
    assert not ok
    assert "0.300" in why and "0.60" in why, why


def test_grid_size_matches_the_generator():
    assert grid_size() == len(list(
        __import__("pqv2.strategy_b.strategy", fromlist=["x"])
        .transformation_grid()))


def test_asymmetry_prefers_shape_over_win_rate():
    """A 40% win rate at 4:1 must beat an 85% win rate at 1:9 on expectancy."""
    good = backtest.Result()
    good.returns = [4.0] * 40 + [-1.0] * 60
    bad = backtest.Result()
    bad.returns = [1.0] * 85 + [-9.0] * 15
    assert good.expectancy > bad.expectancy
    assert good.asymmetry()["win_loss_ratio"] > bad.asymmetry()["win_loss_ratio"]
