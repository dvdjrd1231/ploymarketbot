"""§40 — continuous operational intelligence.

Two things are being tested, and the second is the one that decides whether
anybody keeps this feature switched on:

  1. that real conditions are detected at all, and
  2. that trivial ones stay quiet.

A monitor that surfaces everything is muted within a week, after which it
detects nothing that matters to anyone. So the floor, the ranking and the
deduplication get as much coverage as the detectors do.
"""

from __future__ import annotations

import time

import pytest

from pqv3.agents.console import Console
from pqv3.agents.surface import PRIORITY_FLOOR, Discovery, Surfacer
from pqv3.core.store import Store
from pqv3.server.api import Api


@pytest.fixture
def surf(st) -> Surfacer:
    return Surfacer(st, Store(st))


# ----------------------------------------------------------------- ranking
def test_priority_is_a_product_not_a_sum():
    """An important, urgent finding that cannot cost anything stays quiet."""
    loud = Discovery(key="k", kind="RISK", headline="", measured="",
                     importance=1.0, impact=1.0, urgency=1.0)
    harmless = Discovery(key="k", kind="RISK", headline="", measured="",
                         importance=1.0, impact=0.02, urgency=1.0)
    assert loud.priority == 1.0
    assert harmless.priority < PRIORITY_FLOOR, (
        "a sum would have scored this 2.02 out of 3 and interrupted somebody "
        "over a reading that cannot lose money")


def test_the_three_factors_are_labelled_as_estimates():
    """§24. They order a queue; they are not measurements."""
    d = Discovery(key="k", kind="DATA", headline="h", measured="m",
                  importance=0.5, impact=0.5, urgency=0.5).to_dict()
    assert "ESTIMATE" in d["basis"]
    assert "not a measurement" in d["basis"]


# --------------------------------------------------------------- detectors
def test_a_clean_store_surfaces_nothing(surf):
    """§33. Nothing to report is a legitimate result, not a broken monitor."""
    assert surf.run() == []


def test_collector_failure_is_detected(st, surf):
    surf.store.record_health("news", "ERROR", error="connection refused")
    out = surf.run()
    assert any("news collector is failing" in d["headline"] for d in out)
    d = [x for x in out if "news" in x["headline"]][0]
    assert "connection refused" in d["measured"]
    assert d["priority"] >= PRIORITY_FLOOR


def test_drawdown_is_detected_before_the_stop_not_after(st, surf):
    """Half-way to the hard stop is the useful moment to hear about it."""
    from pqv3.portfolio.capital import account_from_store
    mode = st.mode.value
    surf.store.insert("positions", [{
        "position_id": "p1", "mode": mode, "market_id": "M", "token_id": "T",
        "status": "CLOSED", "size_usdc": 40.0, "entry_price": 0.5,
        "exit_price": 0.1, "realized_pnl": -20.0, "opened_ts": 1,
    }], source="test")
    acct = account_from_store(surf.store, st, mode)
    assert acct.drawdown > 0, "fixture must actually produce a drawdown"

    out = surf.detect()
    hits = [d for d in out if d.key == "drawdown"]
    assert hits, [d.key for d in out]
    assert hits[0].importance == 1.0 and hits[0].impact == 1.0


def test_degraded_strategies_are_surfaced(st, surf):
    surf.store.insert("strategies", [{
        "strategy_id": "S1", "version": 1, "family": "f", "status": "DEGRADED",
        "params": "{}", "evidence_quality": "WEAK",
    }], source="test")
    assert any(d.key.startswith("degraded:") for d in surf.detect())


def test_stale_data_is_only_stale_when_collection_is_on(st, surf):
    """Collectors switched off is 'off', not 'stale'. The audit covers that."""
    old = int(time.time()) - 86_400
    surf.store.insert("news_items", [{
        "uid": "n1", "title": "x", "source_name": "s", "source_class": "WIRE",
        "confirmation": "SINGLE", "reliability": 0.5, "ts": old,
        "capture_ts": old,
    }], source="test")
    st.collectors.enabled = False
    assert not [d for d in surf.detect() if d.key.startswith("stale:")]

    st.collectors.enabled = True
    hits = [d for d in surf.detect() if d.key == "stale:news_items"]
    assert hits and "has stopped arriving" in hits[0].headline


def test_good_news_is_news_too(st, surf, monkeypatch):
    """§40 does not say 'surface only problems'."""
    monkeypatch.setattr(
        "pqv3.ingest.settled_ts.coverage",
        lambda store: {"pit_features_enabled": True, "usable": 900,
                       "total": 1000})
    hits = [d for d in surf.detect() if d.key == "settlement_unlocked"]
    assert hits and "now support" in hits[0].headline


def test_a_broken_detector_reports_itself(st, surf, monkeypatch):
    """A monitor that fails silently reports calm it never checked."""
    def boom():
        raise RuntimeError("detector exploded")
    monkeypatch.setattr(surf, "_collectors", boom)
    out = surf.detect()
    hits = [d for d in out if d.key.startswith("detector_error:")]
    assert hits and "detector exploded" in hits[0].measured


# ------------------------------------------------------------ deduplication
def test_the_same_unchanged_condition_surfaces_once(surf):
    surf.store.record_health("news", "ERROR", error="connection refused")
    assert surf.run()
    assert surf.run() == [], "an unchanged condition must not repeat"


def test_a_changed_value_surfaces_again(surf):
    surf.store.record_health("news", "ERROR", error="connection refused")
    assert surf.run()
    surf.store.record_health("news", "ERROR", error="TLS handshake failed")
    again = surf.run()
    assert any("TLS handshake" in d["measured"] for d in again), (
        "'the drawdown is 8%' and 'the drawdown is 19%' are different facts "
        "wearing the same name")


def test_below_the_floor_is_recorded_but_not_shown(surf, monkeypatch):
    quiet = Discovery(key="quiet", kind="DATA", headline="minor thing",
                      measured="m", importance=0.2, impact=0.2, urgency=0.2)
    monkeypatch.setattr(surf, "detect", lambda: [quiet])
    assert surf.run() == []
    rows = surf.store.query("SELECT * FROM discoveries WHERE key='quiet'")
    assert rows and rows[0]["surfaced"] == 0, (
        "'you never told me' must have an answer either way")


def test_ack_clears_the_queue(surf):
    surf.store.record_health("news", "ERROR", error="connection refused")
    surf.run()
    assert surf.pending()
    assert surf.ack() >= 1
    assert not surf.pending()


# ------------------------------------------------------------- integration
def test_the_console_carries_what_was_noticed(st):
    store = Store(st)
    store.record_health("orderbook", "ERROR", error="connection refused")
    r = Console(st, store).ask("how many wallets", narrate=False)
    assert r["surfaced"], "a discovery with no channel to reach anyone is not "\
                          "a monitor"
    assert any("orderbook" in d["headline"] for d in r["surfaced"])


def test_the_console_does_not_repeat_itself(st):
    store = Store(st)
    store.record_health("orderbook", "ERROR", error="connection refused")
    con = Console(st, store)
    assert con.ask("how many wallets", narrate=False)["surfaced"]
    second = con.ask("how many strategies", narrate=False)["surfaced"]
    # Still visible while unacknowledged, but not re-detected as new.
    assert all(d.get("acked", 0) == 0 for d in second)


def test_discoveries_reach_the_activity_page(st):
    store = Store(st)
    store.record_health("chain", "ERROR", error="no RPC configured")
    Surfacer(st, store).run()
    d = Api(st, store).get("activity")
    assert d["discoveries"]
    assert "ESTIMATES" in d["discoveries_note"]
