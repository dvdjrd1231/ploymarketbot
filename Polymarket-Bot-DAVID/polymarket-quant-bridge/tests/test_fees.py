"""Per-market maker/taker fees (§4). Both reference bots assume zero fees; near
mid-range a taker fee decides whether EV is positive, so it must be modelled."""

from __future__ import annotations

from pqb.adapters.data_adapter import _fee_bps
from pqb.decision.expected_value import EVConfig, evaluate, round_trip_fee_usdc
from pqb.decision.probability import ProbabilityEstimate
from pqb.models import MarketFeatures, OutcomeQuote


def est(p, conf=0.9):
    return ProbabilityEstimate(market_price=0.5, probability=p, confidence=conf)


def mkt(taker=None):
    return MarketFeatures(market_id="m", question="q", taker_fee_bps=taker)


def test_flat_fallback_when_no_market_fee():
    cfg = EVConfig(fee_per_trade_usdc=0.01)
    assert round_trip_fee_usdc(mkt(), 20.0, cfg) == 0.02   # 0.01 in + 0.01 out


def test_per_market_taker_fee_used_when_present():
    cfg = EVConfig(fee_per_trade_usdc=0.01)
    # 200 bps = 2% of notional each way -> 0.02 * 20 * 2 = 0.80
    assert round_trip_fee_usdc(mkt(taker=200.0), 20.0, cfg) == 0.80


def test_fee_bps_normalises_units():
    assert _fee_bps({"takerBaseFee": 0.02}, ("takerBaseFee",)) == 200.0   # fraction
    assert _fee_bps({"takerFeeBps": 150}, ("takerFeeBps",)) == 150.0      # already bps
    assert _fee_bps({}, ("takerFeeBps",)) is None                          # absent
    assert _fee_bps({"takerFeeBps": 0}, ("takerFeeBps",)) == 0.0           # explicit zero


def test_a_heavy_taker_fee_can_flip_ev_negative():
    """A trade that clears the bar at zero fees is rejected once a real taker
    fee is charged — the whole reason to model fees."""
    cfg = EVConfig(min_ev=0.02, min_confidence=0.1, slippage=0.0,
                   fee_per_trade_usdc=0.0)
    quote = OutcomeQuote(token_id="t", outcome="Yes", ask=0.50, bid=0.49,
                         spread=0.01, source="stream", ask_depth=10_000,
                         tick_size=0.01)
    # 6-point edge, $20 stake. Zero fee -> acceptable.
    free = evaluate(quote, mkt(), est(0.56), cfg, stake=20.0)
    assert free.acceptable
    # 500 bps taker fee (5% each way) on a $20 stake = $2 round trip = 10% of
    # stake -> swamps the 6-point edge.
    heavy = evaluate(quote, mkt(taker=500.0), est(0.56), cfg, stake=20.0)
    assert not heavy.acceptable
