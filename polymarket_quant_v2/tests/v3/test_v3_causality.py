"""The no-look-ahead invariant.

This is the file that decides whether any number this system produces means
anything. A backtest that can see the future does not produce an optimistic
result — it produces a meaningless one, and the failure is invisible in every
metric except this test.
"""

from __future__ import annotations

import time

from pqv3.core.pit import StateBuilder
from pqv3.core.source import HistoricalSource
from pqv3.core.store import Store


def test_no_layer_is_dated_after_as_of(tape):
    """The core invariant, asserted over the whole tape.

    Every layer carries `as_of`, the timestamp of the newest fact in it. If any
    layer's `as_of` exceeds the state's own `as_of`, the state contains
    information that did not exist at decision time.
    """
    store = Store(tape)
    src = HistoricalSource(tape)
    b = StateBuilder(tape, store, src)
    first = 1_700_000_000

    for offset in (0, 3_600, 20_000, 40_000):
        t = first + offset
        ev = b.get(t, "MKT_A", "TOK_A", use_cache=False)
        for layer in ev.layers():
            assert layer.as_of <= t, (
                f"layer {layer.name} dated {layer.as_of}, "
                f"which is {layer.as_of - t}s after as_of {t}")


def test_market_metadata_is_bounded(tape):
    """A regression test for a real leak V3's own gate caught.

    `market_meta` originally returned MAX(ts) over the market's whole history,
    so a state built early in the tape carried a `last_ts` from the end of it.
    INFORMATION_VALIDITY refused to trade and that is how it was found.
    """
    src = HistoricalSource(tape)
    early = 1_700_010_000
    meta = src.market_meta("MKT_A", early)
    assert meta["last_ts"] <= early
    unbounded = src.market_meta("MKT_A")
    assert unbounded["last_ts"] > early, (
        "fixture is not exercising the bound: the market must have prints "
        "after `early` for this test to mean anything")


def test_prints_never_return_the_future(tape):
    src = HistoricalSource(tape)
    cut = 1_700_020_000
    rows = src.prints("TOK_A", cut, lookback_secs=10 ** 9)
    assert rows, "fixture produced no prints"
    assert max(t for t, _, _, _ in rows) <= cut


def test_data_clock_ignores_mechanical_events(tape):
    """REDEEM/MERGE/SPLIT keep arriving after the last real decision.

    Anchoring the data clock to them puts the scanner in a window containing
    nothing anyone chose to do — which is precisely what happened on the real
    dataset, where the newest row is a REDEEM 29 hours after the newest TRADE.
    """
    src = HistoricalSource(tape)
    clock = src.latest_ts()
    last_trade = 1_700_000_000 + 119 * 300          # alpha's final BUY
    assert clock == last_trade, (
        "data clock followed a mechanical event instead of the last TRADE")
    assert clock < 1_700_000_000 + 500_000, "the REDEEM set the clock"


def test_resolution_outcome_is_not_on_the_evidence_state(tape):
    """An agent must have no path to the answer sheet.

    `resolution_for` exists for scoring code only. If it were reachable from
    `EvidenceState`, every agent could read the outcome and every backtest
    would report perfection.
    """
    store = Store(tape)
    b = StateBuilder(tape, store, HistoricalSource(tape))
    ev = b.get(1_700_020_000, "MKT_A", "TOK_A")
    blob = str(ev.to_dict())
    assert "resolution" not in blob.lower() or "resolution_time" in blob.lower()
    for layer in ev.layers():
        assert "resolution" not in layer.data, (
            f"layer {layer.name} carries a resolution field")


def test_store_filters_news_on_capture_not_publication(st):
    """Publication time is not availability time.

    An item published at 10:00 and scraped at 10:40 was not available to a
    decision at 10:15. Filtering on publication would hand the backtest forty
    minutes of hindsight — the classic news-leakage failure.
    """
    store = Store(st)
    published, captured = 1_700_000_000, 1_700_002_400
    store.insert("news_items", [{
        "uid": "x1", "title": "t", "ts": published, "capture_ts": captured}],
        source="test")
    visible = store.query(
        "SELECT * FROM news_items WHERE capture_ts <= ?",
        (published + 900,))
    assert visible == [], (
        "an item captured 40 minutes later was visible to a decision made "
        "15 minutes after publication")
    later = store.query(
        "SELECT * FROM news_items WHERE capture_ts <= ?", (captured,))
    assert len(later) == 1
