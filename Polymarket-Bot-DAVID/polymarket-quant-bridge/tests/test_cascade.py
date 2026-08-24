"""The liquidation-cascade hypothesis stays a hypothesis.

The operator's candidate: BTC liquidation -> forced flow -> short directional
move in the 5-minute Polymarket UP/DOWN market. These pin what the capture
layer must guarantee: every event stored with the starting-point thresholds
as FLAGS (never filters), directions recorded but confirmed only by
outcomes, controls kept, sample honesty enforced, and the liq_* columns
riding into the existing discovery engine instead of any hard-coded rule.
"""

from __future__ import annotations

import pytest

from pqb.analytics.cascade import (CascadeStore, aggregates, analyse,
                                   direction_of, momentum_pct,
                                   volatility_pct)


@pytest.fixture()
def store(tmp_path):
    s = CascadeStore(tmp_path / "cascade.sqlite3")
    yield s
    s.close()


def _event(store, ts=1_000.0, side="long", usd=8_000.0, price=60_000.0,
           pm=None):
    return store.record_event(
        ts=ts, symbol="BTCUSDT", side=side, price=price, qty=usd / price,
        usd=usd, qualifying=usd >= 5_000, btc_before=price,
        momentum_30s=0.01, vol_300s=0.02, events_60s=1, usd_60s=usd,
        pm=pm or {"market": "M1", "question": "Bitcoin Up or Down?",
                  "endTs": ts + 200, "timeLeft": 200,
                  "upBid": 0.48, "upAsk": 0.52,
                  "downBid": 0.46, "downAsk": 0.50})


# -- direction: recorded from forced flow, confirmed only by outcomes --------

def test_forced_flow_direction():
    assert direction_of("long") == "down"     # liquidated long = forced sell
    assert direction_of("short") == "up"      # liquidated short = forced buy


def test_direction_test_reports_hit_rate_from_outcomes(store):
    for i in range(3):
        event_id = _event(store, ts=1_000.0 + i * 400, side="long")
        # Two DOWNs (predicted) and one UP (against prediction).
        store.record_outcome(event_id, "DOWN" if i < 2 else "UP",
                             fee=0.0, assumed_spread=0.01)
    data = analyse(store, min_sample=3)
    assert data["long_liq"]["events"] == 3
    assert data["long_liq"]["hitRate"] == pytest.approx(2 / 3, abs=0.01)


# -- hypothetical economics: a right call must pay for its costs -------------

def test_outcome_charges_entry_fee_and_spread(store):
    event_id = _event(store, side="long")               # predicts DOWN
    store.record_outcome(event_id, "DOWN", fee=0.02, assumed_spread=0.01)
    outcome = store.outcomes()[0]
    # Enter DOWN at its ask 0.50; correct pays 1.00; minus fee and half-spread.
    assert outcome["correct"] == 1
    assert outcome["hypo_net"] == pytest.approx(1.0 - 0.50 - 0.02 - 0.005)


def test_wrong_call_loses_the_stake(store):
    event_id = _event(store, side="short")              # predicts UP
    store.record_outcome(event_id, "DOWN", fee=0.0, assumed_spread=0.0)
    outcome = store.outcomes()[0]
    assert outcome["correct"] == 0
    assert outcome["hypo_net"] == pytest.approx(-0.52)  # UP ask, lost


# -- thresholds are qualifying flags, never capture filters ------------------

def test_small_events_are_captured_with_qualifying_false(store):
    small = store.record_event(
        ts=1.0, symbol="BTCUSDT", side="short", price=60_000, qty=0.01,
        usd=600.0, qualifying=False, btc_before=60_000, momentum_30s=0,
        vol_300s=0, events_60s=1, usd_60s=600.0)
    events = store.events()
    assert len(events) == 1 and events[0]["id"] == small
    assert events[0]["qualifying"] == 0     # kept as a control, flagged


# -- the response curve ------------------------------------------------------

def test_responses_measure_signed_moves_from_event_price(store):
    event_id = _event(store, side="long", price=60_000.0)
    store.record_response(event_id, 60, btc_price=59_400.0)   # -1%
    data = analyse(store, min_sample=1)
    row = data["responseCurve"]["60"]
    assert row["n"] == 1
    assert row["meanAbsMovePct"] == pytest.approx(1.0)
    # Long liq predicts DOWN; a fall IS the predicted way -> positive.
    assert row["meanPredictedMovePct"] == pytest.approx(1.0)


def test_baselines_are_the_control_group(store):
    store.record_baseline(ts=1.0, btc_price=60_000, momentum_30s=0.0,
                          vol_300s=0.01, move_60s=0.05, move_180s=0.1)
    data = analyse(store, min_sample=1)
    assert data["baselineWindows"] == 1
    assert data["baselineAbsMove60sPct"] == pytest.approx(0.05)


# -- sample honesty ----------------------------------------------------------

def test_verdict_is_insufficient_until_the_floor(store):
    event_id = _event(store)
    store.record_outcome(event_id, "DOWN", fee=0.0, assumed_spread=0.0)
    data = analyse(store, min_sample=30)
    assert data["verdict"] == "insufficient_sample"
    assert "30" in data["verdictWhy"]


# -- feature aggregates: the hypothesis enters discovery as columns ----------

def test_aggregates_window_and_imbalance():
    now = 10_000.0
    events = [
        {"ts": now - 10, "side": "long", "usd": 6_000.0},
        {"ts": now - 30, "side": "short", "usd": 2_000.0},
        {"ts": now - 120, "side": "long", "usd": 9_000.0},   # in 300s only
        {"ts": now - 400, "side": "short", "usd": 50_000.0}, # too old
    ]
    agg = aggregates(events, now)
    assert agg["liq_long_usd_60s"] == pytest.approx(6_000.0)
    assert agg["liq_short_usd_60s"] == pytest.approx(2_000.0)
    assert agg["liq_events_300s"] == 3.0
    # More long-liq (forced selling) than short-liq -> negative pressure.
    assert agg["liq_imbalance"] == pytest.approx((2_000 - 6_000) / 8_000)


def test_quiet_market_reads_neutral_zero():
    agg = aggregates([], now=1.0)
    assert set(agg) == {"liq_long_usd_60s", "liq_short_usd_60s",
                        "liq_events_300s", "liq_imbalance"}
    assert all(v == 0.0 for v in agg.values())


def test_liq_columns_ride_the_feature_contract():
    from test_features import context, market, quote
    from pqb.features import token_features

    ctx = context()
    ctx.cascade = {"liq_long_usd_60s": 6_000.0, "liq_short_usd_60s": 0.0,
                   "liq_events_300s": 2.0, "liq_imbalance": -1.0}
    row = token_features(market(), quote(), context=ctx)
    assert row["liq_long_usd_60s"] == 6_000.0
    assert row["liq_imbalance"] == -1.0
    # And without a monitor the columns still exist, neutrally zero.
    bare = token_features(market(), quote(), context=None)
    assert bare["liq_events_300s"] == 0.0


# -- buffer maths ------------------------------------------------------------

def test_momentum_and_volatility_from_buffer():
    now = 100.0
    flat = [(now - 60 + i, 60_000.0) for i in range(60)]
    assert momentum_pct(flat, now, 30.0) == pytest.approx(0.0)
    assert volatility_pct(flat, now) == pytest.approx(0.0)
    rising = [(now - 30 + i, 60_000.0 * (1 + 0.0001 * i)) for i in range(30)]
    assert momentum_pct(rising, now, 30.0) > 0
    assert volatility_pct(rising, now, 300.0) >= 0.0
