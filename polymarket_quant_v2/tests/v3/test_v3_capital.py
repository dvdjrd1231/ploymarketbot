"""The $100 capital model.

The brief's hardest constraint is arithmetic, not architecture: a percentage
per-trade cap collides with absolute venue minimums at small bankrolls, and
every one of these tests exists because the wrong answer to that collision is
a plausible-looking number rather than an error.
"""

from __future__ import annotations

import pytest

from pqv3.portfolio.capital import (Account, CapitalEngine, Exposure,
                                    Feasibility)


@pytest.fixture
def acct(st):
    return Account(starting_capital=100.0, cash=100.0, peak_equity=100.0)


def test_defaults_are_one_hundred_dollars(st):
    assert st.capital.starting_capital == 100.00


def test_eleven_capital_concepts_are_distinct(acct):
    """The brief lists eleven quantities that must not be conflated."""
    acct.cash = 60.0
    acct.position_value = 45.0
    acct.realized_pnl = 3.0
    acct.unrealized_pnl = 2.0
    acct.reserved = 10.0
    acct.exposure = Exposure(by_market={"m": 40.0},
                             by_correlation={"e": 40.0},
                             by_wallet={"w": 25.0}, gross=40.0)
    assert acct.equity == 105.0                    # ACCOUNT BALANCE
    assert acct.available_cash == 50.0             # AVAILABLE CASH
    assert acct.reserved == 10.0                   # RESERVED
    assert acct.position_value == 45.0             # POSITION VALUE
    assert acct.unrealized_pnl == 2.0
    assert acct.realized_pnl == 3.0
    assert acct.total_pnl == 5.0
    assert acct.exposure.by_market["m"] == 40.0    # MARKET EXPOSURE
    assert acct.exposure.by_correlation["e"] == 40.0
    assert acct.exposure.by_wallet["w"] == 25.0    # WALLET-COPY EXPOSURE


def test_reserve_is_never_deployable(st, acct):
    eng = CapitalEngine(st)
    risk = acct.risk_capital(st.capital)
    assert risk == pytest.approx(90.0), "10% reserve was deployable"
    r = eng.size(account=acct, probability=0.6, signal_price=0.5,
                 available_liquidity=10_000.0, confidence=1.0)
    assert r.size_usdc <= 100.0 * st.capital.max_fraction_per_trade + 1e-9


def test_capital_infeasible_when_cap_is_below_venue_minimum(st, acct):
    """THE collision. A 1% cap at $100 is $1.00 which cannot buy 5 shares.

    The required answer is a refusal with a written reason, not a rounded-up
    order and not a silently skipped candidate.
    """
    st.capital.max_fraction_per_trade = 0.01       # $1.00 at $100
    st.capital.min_shares = 5.0
    eng = CapitalEngine(st)
    r = eng.size(account=acct, probability=0.9, signal_price=0.80,
                 available_liquidity=10_000.0, confidence=1.0)
    assert r.feasibility is Feasibility.CAPITAL_INFEASIBLE
    assert r.size_usdc == 0.0
    assert "minimum" in r.reason.lower()
    assert "5" in r.reason, "the refusal must state the share minimum it hit"


def test_unmeasured_liquidity_is_refused_not_assumed(st, acct):
    """Zero liquidity means unmeasured, and unmeasured is not infinite."""
    eng = CapitalEngine(st)
    r = eng.size(account=acct, probability=0.9, signal_price=0.5,
                 available_liquidity=0.0, confidence=1.0)
    assert r.feasibility is Feasibility.LIQUIDITY_INFEASIBLE
    assert "assume" in r.reason.lower()


def test_liquidity_caps_the_order(st, acct):
    eng = CapitalEngine(st)
    r = eng.size(account=acct, probability=0.75, signal_price=0.5,
                 available_liquidity=6.0, confidence=1.0)
    if r.ok:
        # fill_ratio_assumption is 0.5, so at most $3.00 is takeable.
        assert r.size_usdc <= 3.0 + 1e-9
        assert r.detail.get("reduced_by_liquidity")


def test_kelly_is_always_fractional(st, acct):
    eng = CapitalEngine(st)
    # A near-certain 0.10 contract has an enormous full-Kelly fraction.
    f = eng.kelly(0.95, 0.10)
    full = (0.95 * 9.0 - 0.05) / 9.0
    assert f == pytest.approx(full * st.capital.kelly_fraction)
    assert f < full


def test_exposure_caps_bind_and_name_themselves(st, acct):
    """A refusal must say WHICH cap bound. 'exposure limit' is unactionable."""
    acct.exposure = Exposure(by_wallet={"0xwhale": 20.0})
    eng = CapitalEngine(st)
    r = eng.size(account=acct, probability=0.7, signal_price=0.5,
                 available_liquidity=10_000.0, confidence=1.0,
                 wallet_followed="0xwhale")
    assert r.feasibility is Feasibility.EXPOSURE_LIMIT
    assert r.detail["binding_cap"] == "wallet-copy"
    assert "0xwhale" in r.reason


def test_position_limit_blocks_before_arithmetic(st, acct):
    acct.open_positions = st.capital.max_open_positions
    eng = CapitalEngine(st)
    r = eng.size(account=acct, probability=0.9, signal_price=0.5,
                 available_liquidity=10_000.0, confidence=1.0)
    assert r.feasibility is Feasibility.POSITION_LIMIT


def test_expected_value_accounts_for_costs(st, acct):
    eng = CapitalEngine(st)
    r = eng.size(account=acct, probability=0.55, signal_price=0.50,
                 available_liquidity=10_000.0, confidence=1.0)
    if r.ok:
        assert r.entry_price > r.signal_price, "slippage was not charged"
        gross = r.size_shares * (1.0 - r.signal_price) * 0.55 - \
            r.size_usdc * 0.45
        assert r.expected_value < gross, (
            "expected value ignored the cost of the worse entry price")


def test_tick_rounding_never_breaches_the_cap(st, acct):
    eng = CapitalEngine(st)
    cap = acct.equity * st.capital.max_fraction_per_trade
    for price in (0.03, 0.17, 0.33, 0.5, 0.66, 0.81, 0.97):
        r = eng.size(account=acct, probability=min(0.99, price + 0.05),
                     signal_price=price, available_liquidity=10_000.0,
                     confidence=1.0)
        assert r.size_usdc <= cap + 1e-6, (
            f"rounding at price {price} produced ${r.size_usdc:.4f}, above "
            f"the ${cap:.2f} cap")


def test_account_is_derived_from_the_ledger_not_cached(store, st):
    """A stored balance that drifts from its own ledger reports a fake return."""
    from pqv3.portfolio.capital import account_from_store
    store.insert("positions", [
        {"position_id": "p1", "mode": "PAPER", "market_id": "m1",
         "opened_ts": 1, "closed_ts": 2, "size_usdc": 5.0,
         "realized_pnl": 2.5, "status": "CLOSED"},
        {"position_id": "p2", "mode": "PAPER", "market_id": "m2",
         "opened_ts": 3, "size_usdc": 4.0, "unrealized_pnl": -0.5,
         "status": "OPEN"}], source="test")
    a = account_from_store(store, st, "PAPER")
    assert a.realized_pnl == pytest.approx(2.5)
    assert a.unrealized_pnl == pytest.approx(-0.5)
    assert a.open_positions == 1
    assert a.cash == pytest.approx(100.0 + 2.5 - 4.0)
    assert a.equity == pytest.approx(98.5 + 4.0 - 0.5)
