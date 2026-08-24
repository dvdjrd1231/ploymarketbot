"""Broad ingestion: normalisation, and the pipeline that consumes it."""

from __future__ import annotations

import time

import pytest

from pqb.adapters.data_adapter import _wallet_trade
from pqb.analytics.anomalies import AnomalyConfig
from pqb.analytics.pipeline import IntelPipeline
from pqb.logs import Log
from pqb.models import MarketFeatures, MarketStatus, OutcomeQuote
from conftest import trade


# --- normalisation (step 2) -------------------------------------------------

def test_a_data_api_row_normalises():
    row = {
        "proxyWallet": "0xABCdef0000000000000000000000000000000001",
        "side": "BUY", "asset": "12345", "conditionId": "0xmarket",
        "size": "250.5", "price": "0.42", "timestamp": "1700000000",
        "outcome": "Yes", "title": "Will it?",
        "transactionHash": "0xdeadbeef",
    }
    t = _wallet_trade(row, source="market")
    assert t.wallet == "0xabcdef0000000000000000000000000000000001"
    assert t.token_id == "12345"
    assert t.market_id == "0xmarket"
    assert t.side == "BUY"
    assert t.ts == 1_700_000_000
    # Notional is computed from shares x price, so it always agrees with what
    # the wallet actually paid rather than trusting a field that may not exist.
    assert round(t.usdc, 2) == round(250.5 * 0.42, 2)


def test_sell_side_is_recognised_however_it_is_spelled():
    for spelling in ("SELL", "sell", "Sell", "s"):
        assert _wallet_trade({"side": spelling, "proxyWallet": "0xa"},
                             source="x").side == "SELL"
    for spelling in ("BUY", "buy", "b", ""):
        assert _wallet_trade({"side": spelling, "proxyWallet": "0xa"},
                             source="x").side == "BUY"


def test_alternate_wallet_keys_are_accepted():
    assert _wallet_trade({"maker": "0xAAA"}, source="x").wallet == "0xaaa"
    assert _wallet_trade({"wallet": "0xBBB"}, source="x").wallet == "0xbbb"


def test_a_junk_row_degrades_rather_than_raises():
    t = _wallet_trade({"price": "not-a-number", "size": None}, source="x")
    assert t.wallet == ""
    assert t.price == 0.0 and t.usdc == 0.0


def test_signed_notional():
    assert trade("0xa", 1, side="BUY", usdc=100.0).signed_usdc == 100.0
    assert trade("0xa", 1, side="SELL", usdc=100.0).signed_usdc == -100.0


# --- the pipeline -----------------------------------------------------------

@pytest.fixture
def pipeline(intel_store):
    return IntelPipeline(store=intel_store, log=Log(),
                         anomaly_config=AnomalyConfig(), cohort_size=5,
                         lookback_days=30.0, refresh_seconds=0)


def market(status=MarketStatus.RESOLVED) -> MarketFeatures:
    m = MarketFeatures(market_id="M1", category="Politics", status=status,
                       end_ts=int(time.time()) - 60)
    m.quotes = {"T1": OutcomeQuote(token_id="T1", outcome="Yes"),
                "T2": OutcomeQuote(token_id="T2", outcome="No")}
    return m


def test_resolutions_are_captured_from_settled_markets(pipeline, intel_store):
    prices = {"T1": 1.0, "T2": 0.0}
    written = pipeline.record_resolutions(
        {"M1": market()}, lambda _m, token: prices.get(token))
    assert written == 2
    # Including the outcome that settled at zero — the losing side is the most
    # informative thing a market says about the wallets that bought it.
    assert intel_store.resolutions() == {"T1": 1.0, "T2": 0.0}


def test_active_markets_are_not_treated_as_settled(pipeline, intel_store):
    pipeline.record_resolutions({"M1": market(status=MarketStatus.ACTIVE)},
                                lambda _m, _t: 1.0)
    assert intel_store.resolutions() == {}


def test_refresh_ranks_every_observed_wallet(pipeline, intel_store):
    now = time.time()
    rows = []
    for w in range(4):
        for i in range(10):
            rows.append(trade(f"0xw{w}", int(now - 86400), token=f"T{w}-{i}",
                              market=f"M{i}", price=0.5))
    intel_store.record_trades(rows)
    for w in range(4):
        for i in range(10):
            intel_store.record_resolution(f"T{w}-{i}", f"M{i}",
                                          1.0 if i < 6 + w else 0.0)

    assert pipeline.refresh(force=True, now=now) is True
    assert pipeline.observed_wallets == 4
    assert len(pipeline.intel) == 4
    assert all(w.rank for w in pipeline.intel.values())
    # Persisted, so a restart resumes with ranks instead of an empty board.
    assert len(intel_store.load_scores()) == 4


def test_refresh_respects_its_cadence(intel_store):
    pipeline = IntelPipeline(store=intel_store, log=Log(), refresh_seconds=600)
    intel_store.record_trades([trade("0xa", int(time.time()))])
    now = time.time()
    assert pipeline.refresh(force=True, now=now) is True
    assert pipeline.refresh(now=now + 10) is False
    assert pipeline.refresh(now=now + 601) is True


def test_detect_persists_what_it_finds(pipeline, intel_store):
    now = time.time()
    # Four distinct wallets into one outcome inside the window.
    rows = [trade(f"0xw{i}", int(now - i * 30), token="T1", usdc=500.0)
            for i in range(5)]
    intel_store.record_trades(rows)
    pipeline.refresh(force=True, now=now)

    found = pipeline.detect({"M1": market(status=MarketStatus.ACTIVE)}, now=now)
    assert any(a.kind == "convergence" for a in found)
    stored = intel_store.recent_anomalies(limit=50)
    assert len(stored) == len(found)
    assert {row["kind"] for row in stored} == {a.kind for a in found}


def test_detection_can_be_turned_off(intel_store):
    cfg = AnomalyConfig(enabled=False)
    pipeline = IntelPipeline(store=intel_store, log=Log(), anomaly_config=cfg,
                             refresh_seconds=0)
    intel_store.record_trades([trade(f"0xw{i}", int(time.time()), usdc=900.0)
                               for i in range(8)])
    assert pipeline.detect({}, now=time.time()) == []


def test_a_restart_reloads_persisted_ranks(intel_store):
    now = time.time()
    rows = [trade("0xa", int(now - 86400), token=f"T{i}", market=f"M{i}")
            for i in range(12)]
    intel_store.record_trades(rows)
    for i in range(12):
        intel_store.record_resolution(f"T{i}", f"M{i}", 1.0 if i < 8 else 0.0)

    first = IntelPipeline(store=intel_store, log=Log(), refresh_seconds=0)
    first.refresh(force=True, now=now)
    assert first.intel["0xa"].rank == 1

    # A fresh pipeline over the same store: cycle 1 has ranks immediately.
    restarted = IntelPipeline(store=intel_store, log=Log())
    assert restarted.intel["0xa"].rank == 1
    assert restarted.observed_wallets == 1


def test_an_active_anomaly_is_recorded_once_but_stays_visible(pipeline,
                                                              intel_store):
    """A detection stays true while its evidence is in the recent window.

    The engine must keep seeing it — it is still live evidence — but recording
    it on every cycle would write one event 180 times an hour and make "how
    often does this fire?" unanswerable from the table meant to answer it.
    """
    now = time.time()
    intel_store.record_trades([
        trade(f"0xw{i}", int(now - i * 30), token="T1", usdc=500.0)
        for i in range(5)])
    pipeline.refresh(force=True, now=now)
    markets = {"M1": market(status=MarketStatus.ACTIVE)}

    first = pipeline.detect(markets, now=now)
    stored_after_first = len(intel_store.recent_anomalies(limit=500))
    assert first and stored_after_first == len(first)

    again = pipeline.detect(markets, now=now + 20)
    # Still visible to the engine...
    assert len(again) == len(first)
    # ...but not written a second time.
    assert len(intel_store.recent_anomalies(limit=500)) == stored_after_first


def test_a_lapsed_anomaly_is_recorded_again_when_it_returns(pipeline,
                                                            intel_store):
    now = time.time()
    intel_store.record_trades([
        trade(f"0xw{i}", int(now - i * 30), token="T1", usdc=500.0)
        for i in range(5)])
    pipeline.refresh(force=True, now=now)
    markets = {"M1": market(status=MarketStatus.ACTIVE)}

    first = pipeline.detect(markets, now=now)
    beyond_window = now + pipeline.cfg.recent_seconds + 1
    # Re-observe so the cluster is inside the window again at the later time.
    intel_store.record_trades([
        trade(f"0xw{i}", int(beyond_window - i * 30), token="T1", usdc=500.0)
        for i in range(5)])
    pipeline.detect(markets, now=beyond_window)
    assert len(intel_store.recent_anomalies(limit=500)) > len(first)


def test_settlement_sweep_targets_markets_with_no_known_outcome(intel_store):
    now = int(time.time())
    intel_store.record_trades([
        trade("0xa", now, token="T1", market="M-known"),
        trade("0xb", now - 500, token="T2", market="M-unknown"),
    ])
    intel_store.record_resolution("T1", "M-known", 1.0)
    pending = intel_store.markets_without_resolution(limit=10)
    assert pending == ["M-unknown"]


def test_a_barren_capture_does_not_burn_the_interval(tmp_path, monkeypatch):
    """A cold start must not starve the research series.

    On the first cycles the book stream has not delivered, so every quote is a
    metadata fallback and nothing is worth capturing. If the timer advanced on
    that barren pass, the next attempt would be a full interval away - and on a
    fresh start that repeats, delaying by hours the series strategy discovery
    depends on.
    """
    from pqb.analytics.store import IntelStore
    from pqb.config import Config
    from pqb.logs import Log
    from pqb.models import (AccountState, BridgeContext, MarketFeatures,
                            MarketStatus, OutcomeQuote)
    from pqb.runner import Runner

    cfg = Config()
    cfg.root = tmp_path
    runner = Runner.__new__(Runner)          # no network, no event loop
    runner.config = cfg
    runner.intel_store = IntelStore(tmp_path / "intel.sqlite3")
    runner._last_capture = 0.0

    def ctx(source):
        m = MarketFeatures(market_id="M1", status=MarketStatus.ACTIVE)
        m.quotes = {"T1": OutcomeQuote(token_id="T1", outcome="Yes",
                                       bid=0.4, ask=0.42, source=source)}
        return BridgeContext(cycle_id="c", ts=time.time(),
                             account=AccountState(), markets={"M1": m})

    # Cold: nothing captured, and the clock must be untouched.
    runner._capture_research(ctx("none"), Log())
    assert runner.intel_store.stats()["research_rows"] == 0
    assert runner._last_capture == 0.0

    # Warm: captured, and only now does the interval start.
    runner._capture_research(ctx("stream"), Log())
    assert runner.intel_store.stats()["research_rows"] == 1
    assert runner._last_capture > 0.0

    # And it then respects the interval rather than writing every cycle.
    runner._capture_research(ctx("stream"), Log())
    assert runner.intel_store.stats()["research_rows"] == 1
    runner.intel_store.close()


def test_a_blind_cycle_is_counted_as_an_error(tmp_path):
    """Seeing no markets must never be reported as a healthy cycle.

    A client's first run showed `errors=0` on every cycle while the bridge
    could not reach Gamma at all - so it traded nothing, captured nothing and
    ranked nobody, and the logs said everything was fine.
    """
    import inspect
    from pqb.runner import Runner
    src = inspect.getsource(Runner.run_cycle)
    # The guard exists, increments the error count, and explains itself.
    assert "if not markets:" in src
    assert "errors += 1" in src.split("if not markets:")[1][:200]
    assert "blind" in src.lower()


def test_the_universe_is_retried_immediately_while_empty(tmp_path):
    """Waiting out the normal interval while blind is five minutes of nothing."""
    import inspect
    from pqb.runner import Runner
    src = inspect.getsource(Runner.run_cycle)
    assert "blind = not self.data.universe" in src
    assert "self._last_universe = 0.0 if not self.data.universe else time.time()" in src


def test_gamma_query_retries_before_giving_up():
    import inspect
    from pqb.adapters.data_adapter import PolymarketDataAdapter
    src = inspect.getsource(PolymarketDataAdapter._query)
    assert "attempts: int = 3" in src
    # And when it does give up it says so at ERROR, not as a passing warning.
    assert "self.log.error" in src
