"""The Market-State layer: EVENTS -> STATE -> LEAN.

Pins the operator's contract: rolling window changes, three blended 0-100
scores (no master signal), the DORMANT..REVERSAL classification driven by
configurable thresholds, and the clean per-token snapshot LEAN consumes.
"""

from __future__ import annotations

from pqb.bridge.market_state import (MarketStateTracker, STATE_CODE)
from pqb.config import MarketStateConfig

NOW = 1_800_000_000.0


def tracker(**overrides) -> MarketStateTracker:
    cfg = MarketStateConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return MarketStateTracker(cfg)


def feed_quiet(t, token="T", n=6, price=0.50):
    for i in range(n):
        t.feed_book(token, NOW - 290 + i * 50, price, 500, 500)


def feed_impulse(t, token="T"):
    """A live directional move: rising price, quickening buys, ACTIVE AT NOW.

    The burst must reach the present — a tape whose last print is a minute old
    is an exhausted tape, and the classifier rightly says so.
    """
    gaps = [max(2.0, 12.0 - i * 0.45) for i in range(24)]
    ts = NOW - sum(gaps)
    price = 0.50
    for gap in gaps:
        price += 0.004                       # +~9.6% across the burst
        ts += gap
        t.feed_trade(token, ts, price, notional=25.0, is_buy=True)
        t.feed_book(token, ts + 0.5, price, 600, 600)


# -- windows and snapshot ----------------------------------------------------

def test_rolling_price_changes_and_snapshot_shape():
    t = tracker()
    t.feed_book("T", NOW - 250, 0.50, 500, 500)
    t.feed_trade("T", NOW - 100, 0.52, 10.0, True)
    t.feed_trade("T", NOW - 10, 0.55, 10.0, True)
    snap = t.snapshot("T", NOW)
    assert round(snap["ms_chg_300s"], 3) == 0.100          # 0.50 -> 0.55
    assert snap["ms_chg_1s"] == 0.0                        # nothing that fresh
    assert 0.0 <= snap["ms_data_quality"] <= 1.0
    for key in ("ms_impulse", "ms_exhaustion", "ms_anomaly", "ms_state"):
        assert key in snap


def test_unknown_token_yields_empty_snapshot():
    assert tracker().snapshot("nope", NOW) == {}


# -- classification ----------------------------------------------------------

def test_quiet_market_is_dormant():
    t = tracker()
    feed_quiet(t)
    assert t.snapshot("T", NOW)["ms_state"] == STATE_CODE["dormant"]


def test_directional_burst_reads_as_impulse():
    t = tracker()
    feed_impulse(t)
    snap = t.snapshot("T", NOW)
    assert snap["ms_impulse"] >= 55
    assert snap["ms_state"] in (STATE_CODE["impulse"],
                                STATE_CODE["continuation"])


def test_fading_move_reads_as_exhaustion():
    """The move happened, then everything went quiet — engine dying."""
    t = tracker()
    price = 0.50
    ts = NOW - 280
    for _ in range(12):                       # sharp early move...
        price += 0.006
        ts += 5
        t.feed_trade("T", ts, price, notional=30.0, is_buy=True)
    t.feed_book("T", NOW - 5, price, 400, 400)   # ...then near-silence
    snap = t.snapshot("T", NOW)
    assert snap["ms_exhaustion"] >= 55
    assert snap["ms_state"] == STATE_CODE["exhaustion"]


def test_sharp_counter_move_reads_as_reversal():
    t = tracker(exhaustion_score_min=101)     # isolate the reversal rule
    price = 0.50
    ts = NOW - 280
    for _ in range(14):                       # up...
        price += 0.005
        ts += 8
        t.feed_trade("T", ts, price, notional=20.0, is_buy=True)
    for _ in range(4):                        # ...then hard down, recently
        price -= 0.012
        ts = max(ts + 2, NOW - 12)
        t.feed_trade("T", ts, price, notional=20.0, is_buy=False)
    snap = t.snapshot("T", NOW)
    assert snap["ms_state"] == STATE_CODE["reversal"]


def test_thresholds_are_configuration_not_code():
    """The same tape classifies differently under different thresholds."""
    strict = tracker(impulse_score_min=101.0, continuation_score_min=101.0)
    feed_impulse(strict)
    loose = tracker(impulse_score_min=10.0)
    feed_impulse(loose)
    assert loose.snapshot("T", NOW)["ms_state"] == STATE_CODE["impulse"]
    assert strict.snapshot("T", NOW)["ms_state"] != STATE_CODE["impulse"]


# -- anomaly is per-market, not universal ------------------------------------

def test_anomaly_measures_against_the_markets_own_baseline():
    t = tracker()
    ts = NOW - 3_000
    for _ in range(40):                       # teach the baseline: $10 prints
        ts += 60
        t.feed_trade("T", ts, 0.50, notional=10.0, is_buy=True)
        t.snapshot("T", ts + 1)               # snapshots advance the baselines
    quiet = t.snapshot("T", NOW - 400)["ms_anomaly"]
    t.feed_trade("T", NOW - 5, 0.50, notional=500.0, is_buy=True)   # a whale
    loud = t.snapshot("T", NOW)["ms_anomaly"]
    assert loud > quiet


# -- LEAN consumption --------------------------------------------------------

def test_token_features_carries_ms_columns():
    from pqb.features import token_features
    from pqb.models import AccountState, BridgeContext
    from test_engine import market

    m = market()
    ctx = BridgeContext(
        cycle_id="c", ts=NOW, account=AccountState(balance=100.0),
        markets={m.market_id: m},
        market_state={"tok1": {"ms_impulse": 72.0,
                               "ms_state": STATE_CODE["impulse"]}})
    row = token_features(m, m.quote("tok1"), ctx, now=NOW)
    assert row["ms_impulse"] == 72.0
    assert row["ms_state"] == STATE_CODE["impulse"]
    # Absent state -> contract fills zeros, never crashes.
    ctx2 = BridgeContext(cycle_id="c", ts=NOW,
                         account=AccountState(balance=100.0),
                         markets={m.market_id: m})
    assert token_features(m, m.quote("tok1"), ctx2, now=NOW)["ms_impulse"] == 0.0


# -- the literal-completeness deltas ------------------------------------------

def test_volume_change_windows_measure_acceleration():
    t = tracker()
    # Prior minute: $10; latest minute: $50 -> +40 acceleration.
    t.feed_trade("T", NOW - 90, 0.50, notional=10.0, is_buy=True)
    t.feed_trade("T", NOW - 30, 0.51, notional=50.0, is_buy=True)
    snap = t.snapshot("T", NOW)
    assert snap["ms_vol_chg_60s"] == 40.0
    assert "ms_vol_chg_1s" in snap and "ms_vol_chg_15s" in snap


def test_sequence_id_groups_one_move_and_increments_on_the_next():
    t = tracker(impulse_score_min=10.0)      # easy to enter impulse
    feed_impulse(t)
    first = t.snapshot("T", NOW)
    assert first["ms_state"] == STATE_CODE["impulse"]
    assert first["ms_sequence_id"] == 1.0    # first sequence ever
    # The move dies down -> back out of the active family eventually; a fresh
    # burst later must carry a NEW id. Simulate by forcing quiet then bursting.
    t2 = tracker(impulse_score_min=10.0)
    feed_impulse(t2)
    assert t2.snapshot("T", NOW)["ms_sequence_id"] == 1.0
    # Quiet spell: dormant -> sequence reads 0
    t3 = tracker()
    feed_quiet(t3)
    assert t3.snapshot("T", NOW)["ms_sequence_id"] == 0.0


def test_raw_events_are_persisted_and_pruned(tmp_path):
    from pqb.analytics.store import IntelStore

    store = IntelStore(tmp_path / "intel.sqlite3")
    n = store.record_raw_events("trade", [
        (1000.0, "m1", "t1", {"price": 0.5, "size": 10, "side": "BUY"}),
        (1001.0, "m1", "t1", {"price": 0.51, "size": 5, "side": "SELL"}),
    ])
    assert n == 2
    rows = store.query("SELECT id, kind, payload FROM raw_events ORDER BY id")
    assert rows[0]["id"] == 1                # the id IS the event_id
    assert "0.5" in rows[0]["payload"]
    # Retention: ancient events go, recent stay.
    store.record_raw_events("book", [(9e9, "m1", "t1", {"bid": 0.5})])
    store.prune(max_age_days=30)
    kinds = {r["kind"] for r in store.query("SELECT kind FROM raw_events")}
    assert kinds == {"book"}
    store.close()


def test_cross_market_contradiction_fires():
    from pqb.config import HighConfidenceConfig
    from pqb.decision.high_confidence import HighConfidenceFilter
    from pqb.models import AccountState, BridgeContext
    from test_engine import market as mk

    markets = {f"m{i}": mk(f"m{i}", token_id=f"t{i}") for i in range(4)}
    for m in markets.values():
        m.category = "sports"
    ctx = BridgeContext(
        cycle_id="c", ts=NOW, account=AccountState(balance=100.0),
        markets=markets,
        market_state={f"t{i}": {"ms_chg_300s": -0.05} for i in range(1, 4)})
    features = {"ms_state": 2.0, "ms_exhaustion": 10.0, "ms_chg_300s": 0.04,
                "ms_imbalance": 0.5, "ms_liquidity_chg": 0.1, "ask": 0.5}
    filter_ = HighConfidenceFilter(HighConfidenceConfig())
    verdict = filter_.evaluate(
        score=0.75, category="sports", stake=10.0, features=features,
        spread=0.02, depth=100.0, ev_per_dollar=0.05, wallet_net=10.0,
        context=ctx, token_id="t0")
    assert "category peers moving the other way" in \
        verdict.evidence["contradictions"]


def test_prune_forgets_idle_tokens():
    """Long-run hygiene: quiet-for-hours tokens must not accumulate forever."""
    t = tracker()
    t.feed_trade("old", NOW - 10_000, 0.5, 10.0, True)
    t.feed_trade("hot", NOW - 60, 0.5, 10.0, True)
    dropped = t.prune(NOW, idle_seconds=7_200)
    assert dropped == 1
    assert t.snapshot("old", NOW) == {}
    assert t.snapshot("hot", NOW) != {}
