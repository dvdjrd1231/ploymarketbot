"""End-to-end behaviour: startup, store provenance, honesty about empty layers.

The recurring assertion across this file is that absence is reported as absence.
A system whose empty tables render as zeros is indistinguishable from a broken
one, and V1's forty thousand identical DO_NOTHING decisions were only ever
diagnosable because they were written down.
"""

from __future__ import annotations

import json

import pytest

from pqv3.core.canon import Availability, EvidenceState
from pqv3.core.store import Store
from pqv3.crash.meter import CrashLevel, read as read_crash
from pqv3.probability.ensemble import Ensemble, Estimate, build


# ------------------------------------------------------------------- store
def test_every_row_carries_provenance(store):
    store.insert("alerts", [{"kind": "k", "message": "m"}], source="unit")
    row = store.query("SELECT * FROM alerts")[0]
    for col in ("ts", "capture_ts", "source", "data_version", "schema_version"):
        assert row[col] is not None, f"{col} missing"
    assert row["source"] == "unit"


def test_provenance_cannot_be_forgotten_by_a_call_site(store):
    """Stamped by `insert`, not by the caller, so no call site can omit it."""
    store.insert("markets", [{"market_id": "m", "question": "q"}],
                 source="unit")
    row = store.query("SELECT * FROM markets")[0]
    assert row["schema_version"] == 3
    assert row["source"] == "unit"


def test_history_span_is_zero_on_an_empty_table(store):
    assert store.history_span_days("book_snapshots") == 0.0


# ------------------------------------------------------------------ layers
def test_absent_layer_is_unavailable_not_zero(st, store):
    from pqv3.core.pit import StateBuilder
    from pqv3.core.source import HistoricalSource
    b = StateBuilder(st, store, HistoricalSource(st))
    ev = b.get(1000, "m", "t")
    assert ev.order_book.availability is not Availability.OK
    assert ev.order_book.data == {}, (
        "an unmeasured book reported values; a zero spread reads as "
        "'measured, and tight'")
    assert "backfill" in ev.order_book.note.lower()


def test_unconfigured_layer_says_not_configured(st, store):
    from pqv3.core.pit import StateBuilder
    from pqv3.core.source import HistoricalSource
    st.collectors.news_feeds = ()
    st.collectors.chain_rpc = ""
    b = StateBuilder(st, store, HistoricalSource(st))
    ev = b.get(1000, "m", "t")
    assert ev.news.availability is Availability.NOT_CONFIGURED
    assert ev.blockchain.availability is Availability.NOT_CONFIGURED


def test_completeness_reflects_reality(st, store):
    from pqv3.core.pit import StateBuilder
    from pqv3.core.source import HistoricalSource
    b = StateBuilder(st, store, HistoricalSource(st))
    ev = b.get(1000, "m", "t")
    assert 0.0 <= ev.completeness <= 1.0
    assert len(ev.missing_layers()) > len(ev.available_layers()), (
        "a fresh install with no collectors should be mostly empty")


# ------------------------------------------------------------ crash meter
def test_crash_meter_with_no_inputs_reports_that(st):
    r = read_crash(EvidenceState(as_of=100))
    assert r.confidence == 0.0
    assert r.level is CrashLevel.NORMAL
    assert "not measuring" in " ".join(r.drivers)


def test_crash_level_is_driven_by_the_strongest_input(st):
    """Averaging would report NORMAL until the fill fails."""
    ev = EvidenceState(as_of=1000)
    ev.price.availability = Availability.OK
    ev.price.data = {"velocity_1h": 0.30, "acceleration": 0.0,
                     "volatility_1h": 0.0}
    r = read_crash(ev)
    assert r.level.rank >= CrashLevel.SEVERE.rank
    assert r.confidence < 1.0, (
        "one alarming input with nothing corroborating it must not be "
        "full confidence")


def test_crash_meter_de_escalates_slowly(st):
    ev = EvidenceState(as_of=1000)
    ev.price.availability = Availability.OK
    ev.price.data = {"velocity_1h": 0.30, "acceleration": 0.0,
                     "volatility_1h": 0.0}
    hot = read_crash(ev)
    ev.price.data = {"velocity_1h": 0.0, "acceleration": 0.0,
                     "volatility_1h": 0.0}
    cooled = read_crash(ev, prior=hot)
    assert cooled.level.rank >= hot.level.rank - 1, "meter snapped back"


# -------------------------------------------------------------- ensemble
def test_disagreement_widens_the_interval_rather_than_cancelling():
    """0.30 and 0.70 average to 0.50, which looks like a confident coin flip."""
    e = Ensemble(market_probability=0.5, estimates=[
        Estimate("a", 0.30, 1.0), Estimate("b", 0.70, 1.0)])
    lo, hi = e.confidence_interval
    assert e.disagreement > 0.5
    assert hi - lo > 0.2, "dispersion did not widen the interval"


def test_agreeing_estimators_never_claim_zero_width():
    e = Ensemble(market_probability=0.5, estimates=[
        Estimate("a", 0.6, 1.0, n=50), Estimate("b", 0.6, 1.0, n=50)])
    lo, hi = e.confidence_interval
    assert hi > lo, "a zero-width interval is a statement about luck"


def test_missing_estimators_are_omitted_not_defaulted(st, store):
    ev = EvidenceState(as_of=1000)
    ev.price.availability = Availability.OK
    ev.price.data = {"last": 0.4, "velocity_1h": 0.0}
    ens = build(ev, {"market_probability": 0.4}, [])
    assert all(e.probability != 0.5 or e.name == "market_implied"
               for e in ens.estimates), (
        "a 0.5 placeholder is an active claim that the outcome is a coin flip")


def test_edge_is_measured_against_the_market_not_the_ensemble():
    e = Ensemble(market_probability=0.4, estimates=[
        Estimate("a", 0.6, 1.0), Estimate("b", 0.62, 1.0)])
    assert e.edge == pytest.approx(e.calibrated_probability - 0.4)


def test_shrinkage_pulls_toward_the_market():
    weak = Ensemble(market_probability=0.5, estimates=[Estimate("a", 0.9, 0.2)])
    strong = Ensemble(market_probability=0.5,
                      estimates=[Estimate("a", 0.9, 1.0),
                                 Estimate("b", 0.9, 1.0),
                                 Estimate("c", 0.9, 1.0)])
    assert weak.calibrated_probability < strong.calibrated_probability, (
        "a single weak estimator moved the estimate as far as three strong "
        "ones")


# ------------------------------------------------------------- correlation
def test_same_event_positions_are_one_bucket():
    from pqv3.portfolio.correlation import compare, correlation_key
    v = compare("Will Team A win?", "Will Team A win by 3+?",
                a_event="E1", b_event="E1")
    assert v.correlated and v.basis == "event_id" and v.strength == 1.0
    assert correlation_key("m1", "E1") == correlation_key("m2", "E1")


def test_shared_entities_are_detected_without_an_event_id():
    from pqv3.portfolio.correlation import compare
    v = compare("Will Arsenal beat Chelsea?", "Arsenal to win the league?")
    assert v.correlated
    assert "arsenal" in v.shared


def test_unrelated_markets_are_not_bucketed():
    from pqv3.portfolio.correlation import compare
    v = compare("Will Bitcoin exceed 100000?",
                "Will Spain win Eurovision?")
    assert not v.correlated


def test_exposure_aggregation_reveals_hidden_concentration():
    from pqv3.portfolio.correlation import aggregate_exposure
    pos = [{"market_id": "m1", "size_usdc": 5.0, "correlation_key": "event:E1"},
           {"market_id": "m2", "size_usdc": 5.0, "correlation_key": "event:E1"},
           {"market_id": "m3", "size_usdc": 5.0, "correlation_key": "event:E2"}]
    b = aggregate_exposure(pos)
    assert b["event:E1"]["usdc"] == 10.0
    assert b["event:E1"]["positions"] == 2


# ---------------------------------------------------------------- startup
def test_startup_runs_all_fifteen_steps(tape):
    from pqv3.runtime import Engine
    eng = Engine(tape)
    steps = eng.start(build_dna=False)
    assert len(steps) == 15
    assert [s.n for s in steps] == list(range(1, 16))
    assert all(s.detail for s in steps), "a step reported nothing"


def test_startup_survives_a_missing_data_source(st):
    from pqv3.runtime import Engine
    eng = Engine(st)                              # data_db does not exist
    steps = eng.start(build_dna=False)
    assert len(steps) == 15, "startup aborted instead of reporting"
    assert eng.st.mode.value == "RESEARCH"


def test_config_check_warns_when_the_cap_cannot_meet_the_minimum(st):
    from pqv3.runtime import Engine
    st.capital.starting_capital = 10.0            # $0.50 per-trade cap
    st.capital.max_fraction_per_trade = 0.05
    eng = Engine(st)
    steps = eng.start(build_dna=False)
    detail = steps[0].detail
    assert "below" in detail and "minimum" in detail, (
        "a bankroll that cannot size any trade started silently")


# --------------------------------------------------------------- settled_ts
def test_settlement_tiers_are_not_blended(store):
    from pqv3.ingest.settled_ts import METHOD_CONFIDENCE, coverage
    assert METHOD_CONFIDENCE["VENUE_REPORTED"] > \
        METHOD_CONFIDENCE["FIRST_OBSERVED"] > METHOD_CONFIDENCE["V1_FALLBACK"]
    store.insert("resolution_times", [
        {"token_id": "t1", "settled_ts": 10, "method": "V1_FALLBACK",
         "confidence": 0.2},
        {"token_id": "t2", "settled_ts": 20, "method": "VENUE_REPORTED",
         "confidence": 1.0}], source="test")
    cov = coverage(store)
    assert cov["total"] == 2
    assert cov["usable"] == 1, "a fallback timestamp counted as usable"
    assert cov["pit_features_enabled"] is False


def test_only_trustworthy_timestamps_override_v1(tape, store):
    from pqv3.core.source import HistoricalSource
    src = HistoricalSource(tape)
    store.insert("resolution_times", [
        {"token_id": "TOK_A", "settled_ts": 999, "method": "V1_FALLBACK",
         "confidence": 0.2}], source="test")
    assert src.use_settlement_times(store) == 0, (
        "a low-confidence fallback was allowed to override V1")


# ------------------------------------------------------------------- api
def test_every_api_section_returns_without_error(st, store):
    from pqv3.server.api import Api
    api = Api(st, store, None)
    assert len(Api.ROUTES) == 22
    for section in Api.ROUTES:
        payload = api.get(section)
        assert "error" not in payload, f"{section}: {payload.get('error')}"
        json.dumps(payload, default=str)


def test_empty_sections_explain_themselves(st, store):
    from pqv3.server.api import Api
    api = Api(st, store, None)
    for section in ("news", "blockchain", "microstructure", "losses",
                    "strategies", "discovery"):
        assert api.get(section).get("note"), (
            f"{section} is empty and says nothing about why")


def test_overview_reports_nulls_not_zeros_before_any_trade(st, store):
    from pqv3.server.api import Api
    ov = Api(st, store, None).get("overview")
    assert ov["completed_trades"] == 0
    assert ov["win_rate"] is None, "an untested win rate rendered as 0"
    assert ov["expectancy"] is None
    assert ov["profit_factor"] is None
    assert ov["starting_capital"] == 100.0
