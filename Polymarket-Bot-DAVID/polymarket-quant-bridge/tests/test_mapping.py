"""
Adapter data mapping: Polymarket's shapes into the bridge's structures.

The adapter's parsing helpers are exercised directly with recorded response
shapes rather than through a live call, so these tests are deterministic and
run offline.
"""

from __future__ import annotations

import json
import time

import pytest

from pqb.models import (
    Action, Decision, MarketFeatures, MarketStatus, OutcomeQuote, PositionView,
    liquidity_bucket, ttr_bucket,
)

pytest.importorskip(
    "pqb.adapters.data_adapter",
    reason="needs the ploymarketbot checkout (see pqb/upstream.py)")

from pqb.adapters.data_adapter import (  # noqa: E402
    PolymarketDataAdapter, _as_list, _category, _f,
)
from pqb.config import Config  # noqa: E402
from pqb.logs import Log  # noqa: E402


# --- Gamma quirks -----------------------------------------------------------

def test_json_encoded_list_fields_are_normalised():
    # Gamma returns these as JSON strings, not arrays.
    assert _as_list('["a", "b"]') == ["a", "b"]
    assert _as_list(["a"]) == ["a"]
    assert _as_list(None) == []
    assert _as_list("not json") == []


def test_category_falls_back_to_the_parent_event_category_only():
    assert _category({"category": "Politics"}) == "Politics"
    assert _category({"events": [{"category": "Sports"}]}) == "Sports"
    assert _category({}) == ""
    # NOT the event title: a per-match title would give nearly every market its
    # own category and make the journal's category grouping meaningless.
    assert _category({"events": [{"title": "Team A vs Team B - Game 2"}]}) == ""


def test_numeric_coercion_tolerates_strings_and_nulls():
    assert _f("12.5") == 12.5
    assert _f(None) == 0.0
    assert _f("nonsense") == 0.0
    assert _f(None, 3.0) == 3.0


# --- quotes -----------------------------------------------------------------

def test_mark_prefers_the_bid_then_the_mid_then_the_last():
    assert OutcomeQuote("t", bid=0.40, mid=0.45, last=0.50).mark == 0.40
    assert OutcomeQuote("t", bid=None, mid=0.45, last=0.50).mark == 0.45
    assert OutcomeQuote("t", bid=None, mid=None, last=0.50).mark == 0.50


def test_an_empty_book_marks_as_unknown_not_zero():
    # A missing quote must never read as a total loss.
    assert OutcomeQuote("t").mark is None
    assert OutcomeQuote("t", bid=0.0, mid=0.0).mark is None


# --- positions --------------------------------------------------------------

def test_position_arithmetic():
    p = PositionView(token_id="t", size=100.0, avg_price=0.40, cur_price=0.50)
    assert p.cost == pytest.approx(40.0)
    assert p.market_value == pytest.approx(50.0)
    assert p.unrealized_pnl == pytest.approx(10.0)
    assert p.return_pct == pytest.approx(0.25)


def test_drawdown_is_measured_from_the_peak():
    p = PositionView(token_id="t", size=10.0, avg_price=0.40, cur_price=0.60,
                     peak_price=0.80)
    assert p.drawdown_from_peak == pytest.approx(0.25)


def test_drawdown_is_zero_without_a_recorded_peak():
    p = PositionView(token_id="t", size=10.0, avg_price=0.40, cur_price=0.20)
    assert p.drawdown_from_peak == 0.0


def test_unvalued_position_falls_back_to_entry_price():
    p = PositionView(token_id="t", size=10.0, avg_price=0.40, cur_price=0.0)
    assert p.market_value == pytest.approx(4.0)


# --- market features --------------------------------------------------------

def test_seconds_to_resolution_never_goes_negative():
    market = MarketFeatures(market_id="m", end_ts=int(time.time()) - 500)
    assert market.seconds_to_resolution == 0.0


def test_only_active_markets_are_tradable():
    assert MarketFeatures("m", status=MarketStatus.ACTIVE).tradable
    assert not MarketFeatures("m", status=MarketStatus.CLOSED).tradable
    assert not MarketFeatures("m", status=MarketStatus.RESOLVED).tradable


# --- held markets are never filtered away -----------------------------------

def _adapter(**filters) -> PolymarketDataAdapter:
    cfg = Config()
    for key, value in filters.items():
        setattr(cfg.markets.filters, key, value)
    return PolymarketDataAdapter(cfg, Log())


def _record(condition_id: str, *, liquidity: float, end_in: float) -> dict:
    from datetime import datetime, timezone
    end = datetime.fromtimestamp(time.time() + end_in, timezone.utc)
    return {
        "conditionId": condition_id,
        "question": "Will it?",
        "closed": False,
        "active": True,
        "liquidityNum": liquidity,
        "volume24hr": 50_000.0,
        "end_date_iso": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "clobTokenIds": '["tokA", "tokB"]',
        "outcomes": '["Yes", "No"]',
    }


def test_a_thin_market_is_filtered_out_when_not_held():
    adapter = _adapter(min_liquidity=10_000.0)
    records = {"m1": _record("m1", liquidity=500.0, end_in=86_400)}
    assert adapter._apply_filters(records, []) == []


def test_a_market_we_hold_survives_the_filters():
    """A filter decides what is worth entering, not what we may still see.

    A held position whose market has gone thin or is near resolution is exactly
    the one that most needs a live price — dropping it would freeze its mark at
    cost and disable every exit rule that depends on the price moving.
    """
    adapter = _adapter(min_liquidity=10_000.0, min_seconds_to_resolution=86_400)
    records = {
        "m1": _record("m1", liquidity=500.0, end_in=60),      # thin AND expiring
        "m2": _record("m2", liquidity=500.0, end_in=60),      # same, not held
    }
    adapter.pin(["m1"], ["tokA"])
    kept = adapter._apply_filters(records, [])
    assert "m1" in kept
    assert "m2" not in kept


def test_pinning_reports_whether_a_refresh_is_needed():
    adapter = _adapter()
    # A market we do not have a record for must force a refresh.
    assert adapter.pin(["m1"], ["tokA"]) is True
    adapter._records = {"m1": _record("m1", liquidity=50_000.0, end_in=86_400)}
    assert adapter.pin(["m1"], ["tokA"]) is False      # already known
    assert adapter.pin(["m1", "m2"], ["tokA"]) is True  # m2 is new


def test_pinned_tokens_are_tracked_for_pricing_immediately():
    adapter = _adapter()
    adapter.pin(["m1"], ["tokA", "tokB"])
    # Waiting for the next universe refresh would leave the position unpriced.
    assert {"tokA", "tokB"} <= adapter.prices._wanted


def test_releasing_a_rotated_out_market_keeps_held_tokens_subscribed():
    """The universe rotates; the subscription must not grow without bound —
    but releasing a market must never un-price a position we still hold."""
    adapter = _adapter()
    adapter.prices.track(["old1", "old2"])
    adapter.pin(["m1"], ["held"])
    # Simulate a refresh that keeps only a new market's tokens.
    adapter.prices.retain_only({"new1"} | adapter._pinned_tokens)
    assert "held" in adapter.prices._wanted
    assert "new1" in adapter.prices._wanted
    assert "old1" not in adapter.prices._wanted


# --- journal tags -----------------------------------------------------------

def test_liquidity_buckets():
    assert liquidity_bucket(500_000) == "deep"
    assert liquidity_bucket(50_000) == "normal"
    assert liquidity_bucket(8_000) == "thin"
    assert liquidity_bucket(100) == "illiquid"


def test_time_to_resolution_buckets():
    assert ttr_bucket(None) == "unknown"
    assert ttr_bucket(1_800) == "<1h"
    assert ttr_bucket(5 * 3600) == "1-6h"
    assert ttr_bucket(20 * 3600) == "6-24h"
    assert ttr_bucket(3 * 86400) == "1-7d"
    assert ttr_bucket(60 * 86400) == ">30d"


# --- decisions --------------------------------------------------------------

def test_only_trade_actions_are_actionable():
    for action in (Action.BUY, Action.SELL, Action.REDUCE, Action.EXIT):
        assert Decision(action=action).is_actionable
    for action in (Action.HOLD, Action.NOTHING):
        assert not Decision(action=action).is_actionable


def test_decision_serialises_for_the_journal():
    decision = Decision(action=Action.EXIT, token_id="t", score=0.7,
                        rationale={"why": "stop"}, exit_style="stop")
    payload = decision.to_dict()
    assert payload["action"] == "EXIT"
    assert payload["exitStyle"] == "stop"
    # The journal stores the rationale as JSON, so it has to survive the trip.
    assert json.loads(json.dumps(payload))["rationale"]["why"] == "stop"
