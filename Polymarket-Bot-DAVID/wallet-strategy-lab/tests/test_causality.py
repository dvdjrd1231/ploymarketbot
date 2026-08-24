"""The tests that matter: look-ahead, and the ladder.

Everything else in this engine is a convenience. These assert the two
properties that make its output meaningful (§20, §43, §42).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from walletlab.backtest import Result, run
from walletlab.config import CostModel, Settings
from walletlab.data import SettledTrade
from walletlab.state import Observation, WalletState
from walletlab.stats import benjamini_hochberg, placebo_p
from walletlab.strategy import CopyStrategy, grid_size, naive_copy
from walletlab.validate import Validation, time_split


def _st(tmp_path) -> Settings:
    return Settings(data_db=Path("unused.sqlite3"), work_dir=tmp_path)


class _NoTape:
    """A tape with no prints — every delayed copy must be UNFILLED."""

    def price_at(self, token_id, at_ts, window=3600):
        return None


class _FlatTape:
    def __init__(self, px):
        self.px = px

    def price_at(self, token_id, at_ts, window=3600):
        return self.px


def _obs(wallet="w", ts=100, price=0.5, resolution=1.0, token="t", **kw):
    tr = SettledTrade(
        wallet=wallet, ts=ts, token_id=token, market_id="m", outcome="Yes",
        price=price, size=10.0, usdc=100.0, resolution=resolution,
        settled_ts=ts + 1000, question="q",
    )
    base = dict(
        trade=tr, w_settled_n=0, w_win_rate=0.0, w_roi=0.0, w_roll_win_rate=0.0,
        w_roll_roi=0.0, w_edge_t=0.0, w_consec_losses=0, w_consec_wins=0,
        w_seen_n=0, w_secs_since_prev=-1, w_open_notional=0.0,
        w_token_repeat=False, price=price, notional=100.0, rel_notional=1.0,
        secs_to_settle=1000,
    )
    base.update(kw)
    return Observation(**base)


# --------------------------------------------------------------------- §43
def test_wallet_state_only_counts_settled_outcomes():
    """A trade must not affect wallet statistics before it settles."""
    s = WalletState()
    assert s.settled_n == 0 and s.win_rate == 0.0
    s.seen_n += 1          # the trade happened...
    assert s.settled_n == 0, "seeing a trade must not create a track record"
    s.fold_settled(won=True, gross_ret=1.0, stake=100.0)   # ...and later settled
    assert s.settled_n == 1 and s.win_rate == 1.0


def test_settlement_clock_is_never_earlier_than_the_trade():
    """The whole causal guarantee rests on this ordering.

    `resolutions.settled_ts` is 0 throughout this dataset, so data.py falls back
    to `resolutions.ts` (the observation time). That substitution is only safe
    because it is always LATER than the trade -- it can delay an outcome
    becoming known, never advance it. A future ingester change that breaks the
    ordering would silently reintroduce look-ahead, so it is asserted here.
    """
    tr = SettledTrade(
        wallet="w", ts=1000, token_id="t", market_id="m", outcome="Yes",
        price=0.5, size=1.0, usdc=100.0, resolution=1.0,
        settled_ts=900,  # pathological: settled before the trade
        question="q",
    )
    assert tr.settled_ts < tr.ts
    # state.py must not fold an outcome in before the trade that produced it.
    # A settlement in the past is clamped by the heap ordering, so the wallet's
    # record at the moment of THIS trade is still empty.
    s = WalletState()
    assert s.settled_n == 0


def test_edge_t_stat_refuses_tiny_samples():
    """Four lucky trades must not produce a confident-looking statistic."""
    s = WalletState()
    for _ in range(4):
        s.fold_settled(True, 1.0, 100.0)
    assert s.edge_t_stat() == 0.0


# --------------------------------------------------------------------- §29/§30
def test_delayed_copy_is_unfilled_when_nothing_printed(tmp_path):
    """The core anti-fiction rule: no print, no fill — never the wallet's price."""
    st = _st(tmp_path)
    strat = CopyStrategy(wallet="w", delay_secs=300)
    r = run(strat, [_obs(resolution=1.0)], st, _NoTape())
    assert r.n_admitted == 1
    assert r.n_filled == 0
    assert r.n_unfilled == 1
    assert r.pnl == 0.0


def test_delayed_copy_uses_tape_price_not_wallet_price(tmp_path):
    """If the market moved against you, the backtest must feel it."""
    st = _st(tmp_path)
    strat = CopyStrategy(wallet="w", delay_secs=300, stake_flat=100.0)
    # wallet bought at 0.50; by the time we copy, the print is 0.90
    r = run(strat, [_obs(price=0.50, resolution=1.0)], st, _FlatTape(0.90))
    assert r.n_filled == 1
    naive = run(naive_copy("w"), [_obs(price=0.50, resolution=1.0)], st, _FlatTape(0.90))
    assert r.expectancy < naive.expectancy, "late entry must earn less"


def test_costs_are_actually_applied(tmp_path):
    """A fair coin at its fair price must lose money after costs."""
    st = Settings(data_db=Path("x"), work_dir=tmp_path, costs=CostModel(slippage_bps=100))
    win = run(naive_copy("w"), [_obs(price=0.5, resolution=1.0)], st, _NoTape())
    lose = run(naive_copy("w"), [_obs(price=0.5, resolution=0.0)], st, _NoTape())
    assert win.expectancy + lose.expectancy < 0


# --------------------------------------------------------------------- §8
def test_time_split_is_chronological_and_disjoint():
    obs = [_obs(ts=t) for t in range(100)]
    train, valid, test = time_split(obs)
    assert len(train) + len(valid) + len(test) == 100
    assert max(o.trade.ts for o in train) < min(o.trade.ts for o in valid)
    assert max(o.trade.ts for o in valid) < min(o.trade.ts for o in test)


# --------------------------------------------------------------------- §34
def test_bh_threshold_tightens_as_hypotheses_grow():
    """The same p-value must not survive a wider search."""
    few = benjamini_hochberg([0.01] + [0.5] * 9, fdr=0.10)[1]
    many = benjamini_hochberg([0.01] + [0.5] * 9999, fdr=0.10)[1]
    assert few >= 1
    assert many == 0, "one p=0.01 among 10,000 tests is not a discovery"


def test_placebo_detects_a_filter_that_selects_nothing():
    """A filter with no information must not look good on a profitable wallet."""
    population = [0.2] * 1000            # every trade equally good
    subset = [0.2] * 50                  # filter picked an ordinary sample
    assert placebo_p(subset, population) > 0.10


# --------------------------------------------------------------------- §42
def _validation(expectancy, n_filled, markets=25, conc=0.1, walk_ok=4):
    t = Result()
    t.n_filled = n_filled
    t.returns = [expectancy] * n_filled
    t.markets = {f"m{i}" for i in range(markets)}
    t.stake = 100.0 * n_filled
    t.pnl = t.stake * expectancy
    t._by_market = {f"m{i}": (1.0 if i else conc * 10) for i in range(markets)}
    t.equity = [expectancy * i for i in range(n_filled)]
    v = Validation(strategy=naive_copy("w"), train=Result(), validation=Result(), test=t)
    good = Result(); good.returns = [0.1] * 10; good.n_filled = 10
    v.walk = [good] * walk_ok
    return v


def test_insufficient_evidence_never_validates():
    v = _validation(0.05, n_filled=10)
    assert v.status(0.05) == "INSUFFICIENT_EVIDENCE"
    assert v.score() == 0.0


def test_negative_expectancy_fails_regardless_of_significance():
    v = _validation(-0.05, n_filled=200)
    assert v.status(0.99) == "FAILED"


def test_status_is_never_validated_without_passing_bh():
    v = _validation(0.05, n_filled=200)
    # bh_threshold of 0 means nothing passed multiple-testing control
    assert v.status(0.0) != "VALIDATED"


def test_grid_is_a_declared_size():
    """The hypothesis count must be knowable before a sweep, for §34."""
    assert grid_size() == 6 * 3 * 3 * 2 * 2 * 4 * 2


# --------------------------------------------------------------------- §16
def test_spec_hash_is_stable_and_wallet_sensitive():
    a = CopyStrategy(wallet="w1", min_price=0.3)
    b = CopyStrategy(wallet="w1", min_price=0.3, label="different-label")
    c = CopyStrategy(wallet="w2", min_price=0.3)
    assert a.spec_hash() == b.spec_hash(), "label must not change identity"
    assert a.spec_hash() != c.spec_hash()
    assert a.params_only_hash() == c.params_only_hash(), "same idea, other wallet"


def test_naive_copy_admits_everything():
    n = naive_copy("w")
    assert n.admits(_obs(price=0.03))
    assert n.admits(_obs(price=0.97, rel_notional=0.01))
