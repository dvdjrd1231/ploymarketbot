"""
Order sizing and exchange-rule validation.

These are the checks that stand between a decision and an order the exchange
would reject — or, worse, accept for the wrong amount.
"""

from __future__ import annotations

from pqb.adapters.sizing import (
    effective_min_usdc, floor_shares, round_price_down, round_price_up,
    size_buy, size_sell,
)


# --- tick rounding ----------------------------------------------------------

def test_buy_price_rounds_up_to_the_tick():
    # Rounding a buy limit down would sit below the ask and never fill.
    assert round_price_up(0.512, 0.01) == 0.52
    assert round_price_up(0.51, 0.01) == 0.51


def test_sell_price_rounds_down_to_the_tick():
    assert round_price_down(0.518, 0.01) == 0.51
    assert round_price_down(0.52, 0.01) == 0.52


def test_prices_stay_inside_the_zero_to_one_domain():
    # 0 and 1 are settlement values, never quotes.
    assert round_price_up(0.999, 0.01) == 0.99
    assert round_price_down(0.0001, 0.01) == 0.01
    assert round_price_up(1.5, 0.01) == 0.99


def test_finer_tick_sizes_are_honoured():
    assert round_price_up(0.0123, 0.001) == 0.013
    assert round_price_down(0.0129, 0.001) == 0.012


def test_missing_tick_falls_back_to_one_cent():
    assert round_price_up(0.512, 0) == 0.52


def test_shares_are_floored_never_rounded_up():
    # Rounding shares up would spend more cash than was authorised.
    assert floor_shares(10.999) == 10.99
    assert floor_shares(10.0) == 10.0
    assert floor_shares(-5) == 0.0


# --- minimum notional -------------------------------------------------------

def test_progression_minimum_wins_when_larger():
    assert effective_min_usdc(1.0, 2.11) == 2.11


def test_config_minimum_wins_when_larger():
    assert effective_min_usdc(5.0, 0.19) == 5.0


# --- buys -------------------------------------------------------------------

def test_simple_buy():
    result = size_buy(desired_usdc=10.0, ask=0.50, tick=0.01,
                      available_cash=100.0, min_usdc=1.0)
    assert result.ok
    assert result.price == 0.50
    assert result.shares == 20.0
    assert result.usdc == 10.0


def test_buy_without_an_ask_is_refused():
    result = size_buy(10.0, None, 0.01, 100.0, 1.0)
    assert not result.ok
    assert "ask" in result.reason.lower()


def test_buy_is_capped_by_available_cash():
    result = size_buy(desired_usdc=100.0, ask=0.50, tick=0.01,
                      available_cash=7.0, min_usdc=1.0)
    assert result.ok
    assert result.usdc <= 7.0
    assert result.shares == 14.0


def test_cash_reserve_is_not_spent():
    result = size_buy(desired_usdc=100.0, ask=0.50, tick=0.01,
                      available_cash=100.0, min_usdc=1.0,
                      reserve_fraction=0.10)
    assert result.ok
    assert result.usdc <= 90.0


def test_buy_below_the_minimum_notional_is_refused():
    result = size_buy(desired_usdc=0.50, ask=0.50, tick=0.01,
                      available_cash=100.0, min_usdc=2.11)
    assert not result.ok
    assert "minimum" in result.reason.lower()


def test_buy_above_the_price_cap_is_refused():
    result = size_buy(10.0, 0.97, 0.01, 100.0, 1.0, max_price=0.95)
    assert not result.ok
    assert "above" in result.reason.lower()


def test_exchange_minimum_share_count_is_met_when_affordable():
    result = size_buy(desired_usdc=1.0, ask=0.10, tick=0.01,
                      available_cash=100.0, min_usdc=0.5,
                      market_min_shares=15.0)
    assert result.ok
    assert result.shares >= 15.0


def test_unaffordable_exchange_minimum_is_refused_not_truncated():
    result = size_buy(desired_usdc=10.0, ask=0.50, tick=0.01,
                      available_cash=2.0, min_usdc=0.5,
                      market_min_shares=100.0)
    assert not result.ok
    assert "minimum" in result.reason.lower()


def test_rounded_up_price_never_overspends():
    # 0.333 rounds to 0.34, so the bill is higher than the naive division.
    result = size_buy(desired_usdc=10.0, ask=0.333, tick=0.01,
                      available_cash=10.0, min_usdc=1.0)
    assert result.ok
    assert result.price == 0.34
    assert result.usdc <= 10.0 + 1e-9


def test_dust_desire_rounds_to_zero_shares_and_is_refused():
    result = size_buy(desired_usdc=0.001, ask=0.90, tick=0.01,
                      available_cash=100.0, min_usdc=0.0001)
    assert not result.ok


# --- sells ------------------------------------------------------------------

def test_full_exit():
    result = size_sell(shares_held=25.0, bid=0.60, tick=0.01)
    assert result.ok
    assert result.shares == 25.0
    assert result.price == 0.60


def test_partial_reduce():
    result = size_sell(shares_held=25.0, bid=0.60, tick=0.01, fraction=0.5)
    assert result.ok
    assert result.shares == 12.5


def test_exit_is_not_blocked_by_a_minimum_notional():
    # A tiny position must still be closable; the alternative is holding a
    # losing tail forever because it became too small to exit.
    result = size_sell(shares_held=0.40, bid=0.02, tick=0.01)
    assert result.ok
    assert result.shares == 0.40


def test_reduce_leaving_unsellable_dust_becomes_a_full_exit():
    result = size_sell(shares_held=10.0, bid=0.50, tick=0.01, fraction=0.6,
                       market_min_shares=5.0)
    assert result.ok
    assert result.shares == 10.0     # the 4-share remainder could not be sold


def test_reduce_leaving_a_sellable_remainder_stays_partial():
    result = size_sell(shares_held=100.0, bid=0.50, tick=0.01, fraction=0.5,
                       market_min_shares=5.0)
    assert result.ok
    assert result.shares == 50.0


def test_sell_without_a_bid_is_refused():
    assert not size_sell(10.0, None, 0.01).ok


def test_selling_nothing_is_refused():
    assert not size_sell(0.0, 0.50, 0.01).ok


# --- fees: small in absolute terms, large against the first progression steps

def test_fee_drag_is_round_trip():
    from pqb.adapters.sizing import fee_drag
    # A position is paid for twice - once in, once out.
    assert fee_drag(1.00, 0.01) == 0.02


def test_fee_drag_is_brutal_at_the_first_progression_step():
    from pqb.adapters.sizing import fee_drag
    # $0.19 is step 1 of David's progression. A 1c fee costs 10.5% round trip,
    # so the position must gain over a tenth just to break even.
    assert round(fee_drag(0.19, 0.01), 3) == 0.105
    # ...and becomes negligible by the end of the progression.
    assert fee_drag(6.07, 0.01) < 0.004


def test_zero_stake_is_infinitely_expensive():
    from pqb.adapters.sizing import fee_drag
    assert fee_drag(0.0, 0.01) == float("inf")
