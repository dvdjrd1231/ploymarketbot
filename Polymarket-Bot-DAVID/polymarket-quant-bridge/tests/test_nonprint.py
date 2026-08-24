"""The non-print engine integration (the operator's core architecture).

His DualNonPrintEngine — imported from qc_lean_bridge, unmodified — sits
between the Polymarket feed and the LEAN bridge, and its structural snapshot
rides on every row both discovery and the live engine read. These tests pin
the adapter's contract: honest prints only, no fabricated ticks, np_-prefixed
columns, and graceful absence when the bridge checkout is missing.
"""

from __future__ import annotations

import pytest

from pqb.bridge.nonprint_feed import NonPrintFeed
from pqb.models import BridgeContext, AccountState
from pqb.features import token_features


def _feed() -> NonPrintFeed:
    feed = NonPrintFeed(print_lookback_s=900, void_max_age_s=3600)
    if not feed.available:
        pytest.skip("qc_lean_bridge not reachable in this environment")
    return feed


def test_structure_is_detected_from_polymarket_shaped_data():
    """A quote jump that skips unprinted levels is exactly what the engine
    exists to see — and it must see it through our adapter."""
    feed = _feed()
    feed.feed_trade("t", ts=1000, price=0.50, bid=0.50, ask=0.51,
                    bid_size=100, ask_size=120, tick_size=0.01)
    feed.feed_quote("t", ts=1010, bid=0.54, ask=0.55, bid_size=80,
                    ask_size=90, tick_size=0.01)
    snap = feed.snapshot("t", 1020)
    assert snap, "engine produced no snapshot"
    assert all(k.startswith("np_") for k in snap)
    assert snap.get("np_ask_skipped_prices", 0) >= 3   # 0.52, 0.53, 0.54


def test_no_tick_is_fabricated_before_the_first_print():
    """Quote-only updates before any trade carry no honest price; nothing is
    sent, so no level is falsely recorded as printed."""
    feed = _feed()
    feed.feed_quote("t", ts=1000, bid=0.50, ask=0.51, bid_size=10,
                    ask_size=10, tick_size=0.01)
    assert feed.snapshot("t", 1001) == {}


def test_idle_quotes_are_not_refed():
    """An unchanged book must not refresh the print window on stale levels."""
    feed = _feed()
    feed.feed_trade("t", ts=1000, price=0.50, bid=0.50, ask=0.51,
                    bid_size=10, ask_size=10, tick_size=0.01)
    feed.feed_quote("t", ts=1010, bid=0.50, ask=0.51, bid_size=10,
                    ask_size=10, tick_size=0.01)
    count_a = feed.snapshot("t", 1011).get("np_ask_nonprint_count", 0)
    # Same book again: dropped before the engine ever sees it.
    feed.feed_quote("t", ts=1020, bid=0.50, ask=0.51, bid_size=10,
                    ask_size=10, tick_size=0.01)
    count_b = feed.snapshot("t", 1021).get("np_ask_nonprint_count", 0)
    assert count_a == count_b


def test_token_features_carries_the_structural_columns():
    """Capture and live evaluation read token_features — the np_ columns must
    arrive there, keyed per token, exactly as the runner attaches them."""
    from test_engine import market  # reuse the fixture helpers

    m = market()
    quote = m.quote("tok1")
    context = BridgeContext(
        cycle_id="c", ts=0.0, account=AccountState(balance=100.0),
        markets={m.market_id: m},
        nonprint={"tok1": {"np_ask_skipped_prices": 3.0}})
    row = token_features(m, quote, context)
    assert row["np_ask_skipped_prices"] == 3.0
    # Another token gets nothing — absence, not zero structure.
    context2 = BridgeContext(
        cycle_id="c", ts=0.0, account=AccountState(balance=100.0),
        markets={m.market_id: m}, nonprint={})
    assert "np_ask_skipped_prices" not in token_features(m, quote, context2)


def test_missing_bridge_degrades_to_inert(monkeypatch):
    """No qc_lean_bridge -> no columns, no crash, bot unchanged."""
    import pqb.bridge.nonprint_feed as npf

    monkeypatch.setattr(npf.NonPrintFeed, "_load_factory", lambda self: None)
    feed = npf.NonPrintFeed()
    assert not feed.available
    feed.feed_trade("t", ts=1, price=0.5, bid=0.5, ask=0.51, bid_size=1,
                    ask_size=1, tick_size=0.01)
    assert feed.snapshot("t", 2) == {}


def test_historical_replayer_adds_np_columns():
    """The best-effort history pass: prints-only, degraded, but present."""
    from pqb.analytics.history_series import _series_for

    # 120 prints: enough to buy MIN_USEFUL_ROWS buckets at three prints each.
    # A 40-print tape is no longer built at all — it cannot fill a series
    # without padding, and padding is what fabricates flat stretches.
    trades = [{"ts": 1000 + i * 60, "price": 0.40 + (i % 5) * 0.03,
               "size": 10.0, "usdc": 4.0, "side": "BUY", "wallet": f"w{i}"}
              for i in range(120)]
    series = _series_for("tokH", trades, scores={})
    assert series, "no rows built"
    with_np = [r for r in series if any(k.startswith("np_") for k in r)]
    if NonPrintFeed().available:
        assert with_np, "np_ columns missing from historical rows"
    else:
        assert not with_np


def test_prune_forgets_idle_tokens():
    feed = _feed()
    feed.feed_trade("old", ts=1000, price=0.5, bid=0.5, ask=0.51,
                    bid_size=1, ask_size=1, tick_size=0.01)
    feed.feed_trade("hot", ts=9_000, price=0.5, bid=0.5, ask=0.51,
                    bid_size=1, ask_size=1, tick_size=0.01)
    dropped = feed.prune(now=10_000, idle_seconds=7_200)
    assert dropped == 1
    assert feed.snapshot("old", 10_001) == {}
    assert feed.snapshot("hot", 10_001) != {}
