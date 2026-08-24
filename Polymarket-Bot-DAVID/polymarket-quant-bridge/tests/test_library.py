"""The persistent strategy library: additive forever, never a reset.

The operator's spec, pinned mechanically: hourly discovery must ADD to the
record — same-rule re-discovery touches the existing row, changed thresholds
become a NEW VERSION earning trust from zero, evidence accumulates one
INDEPENDENT market at a time (a market never testifies twice), demotion is
GRADUAL (validated -> watch -> degraded -> retired), and nothing is ever
erased — retired and rejected rows stay on record permanently.
"""

from __future__ import annotations

import pytest

from pqb.config import ResearchConfig
from pqb.library import StrategyLibrary, next_status


RULE_V1 = {"direction": "long", "entry_feature": "price_z", "entry_op": "<",
           "entry_threshold": -1.0, "stop_pct": 5.0}
RULE_V2 = {**RULE_V1, "entry_threshold": -1.5}   # same family, new thresholds


@pytest.fixture()
def lib(tmp_path):
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    yield library
    library.close()


def _cfg() -> ResearchConfig:
    return ResearchConfig()


# -- intake and versioning -----------------------------------------------------

def test_same_rule_rediscovered_touches_not_duplicates(lib):
    a = lib.upsert_candidate("sigA", RULE_V1, "impulse long", in_score=1.0)
    b = lib.upsert_candidate("sigA", RULE_V1, "impulse long", in_score=2.0)
    assert a == b == "sigA#v1"
    assert len(lib.all_strategies()) == 1


def test_new_thresholds_become_a_new_version_from_zero(lib):
    v1 = lib.upsert_candidate("sigA", RULE_V1, "impulse long")
    lib.record_validation(v1, "M1", trades=20, wins=12, pnl=3.0, drawdown=1.0)
    v2 = lib.upsert_candidate("sigA", RULE_V2, "impulse long (tuned)")
    assert v1 == "sigA#v1" and v2 == "sigA#v2"
    # The "optimisation" inherits NOTHING — it must earn its own record.
    assert lib.cumulative(v2)["trades"] == 0
    assert lib.cumulative(v1)["trades"] == 20
    assert len(lib.all_strategies()) == 2


def test_versions_survive_reopen(lib, tmp_path):
    v1 = lib.upsert_candidate("sigA", RULE_V1, "impulse long")
    lib.record_validation(v1, "M1", trades=10, wins=6, pnl=1.0, drawdown=0.5)
    lib.set_status(v1, "validated")
    lib.close()
    reopened = StrategyLibrary(tmp_path / "library.sqlite3")
    try:
        rows = reopened.all_strategies()
        assert rows[0]["id"] == "sigA#v1"
        assert rows[0]["status"] == "validated"
        assert reopened.cumulative(v1)["trades"] == 10
    finally:
        reopened.close()


# -- cumulative evidence: independent markets only -----------------------------

def test_a_market_never_testifies_twice(lib):
    sid = lib.upsert_candidate("sigA", RULE_V1, "r")
    assert lib.record_validation(sid, "M1", trades=10, wins=6, pnl=2.0,
                                 drawdown=0.5) is True
    # Replaying the SAME market must not inflate the record.
    assert lib.record_validation(sid, "M1", trades=99, wins=99, pnl=99.0,
                                 drawdown=0.0) is False
    cum = lib.cumulative(sid)
    assert cum["trades"] == 10 and cum["markets"] == 1


def test_cumulative_grows_across_markets_never_resets(lib):
    sid = lib.upsert_candidate("sigA", RULE_V1, "r")
    for i in range(5):
        lib.record_validation(sid, f"M{i}", trades=10, wins=6, pnl=1.5,
                              drawdown=0.4)
    cum = lib.cumulative(sid)
    assert cum["markets"] == 5 and cum["trades"] == 50 and cum["wins"] == 30
    assert cum["expectancy"] == pytest.approx(7.5 / 50)
    assert cum["win_rate"] == pytest.approx(0.6)
    assert lib.tested_markets(sid) == {f"M{i}" for i in range(5)}


def test_period_spans_endpoints_not_concatenates(lib):
    sid = lib.upsert_candidate("sigA", RULE_V1, "r")
    lib.record_validation(sid, "M1", trades=5, wins=3, pnl=1.0, drawdown=0.1,
                          period="2026-01-01 .. 2026-02-01")
    lib.record_validation(sid, "M2", trades=5, wins=3, pnl=1.0, drawdown=0.1,
                          period="2026-03-01 .. 2026-04-01")
    assert lib.cumulative(sid)["period"] == "2026-01-01 .. 2026-04-01"


def test_zero_trade_markets_do_not_count_as_evidence(lib):
    """A zero-trade replay adds no evidence AND no longer spends the market.

    This test used to assert that M1 was 'remembered (won't be re-tested)'.
    That was the burn: the market was consumed by a non-observation. A rule
    that did not fire has not been tested there, so M1 stays available and the
    non-observation is recorded as an attempt instead.
    """
    from pqb.library import ATTEMPT_NO_TRADES

    sid = lib.upsert_candidate("sigA", RULE_V1, "r")
    lib.record_attempt(sid, "M1", ATTEMPT_NO_TRADES)
    lib.record_validation(sid, "M2", trades=10, wins=7, pnl=2.0, drawdown=0.2)
    cum = lib.cumulative(sid)
    assert cum["markets"] == 1 and cum["trades"] == 10
    assert lib.evidence_markets(sid) == {"M2"}
    assert lib.excluded_markets(sid) == {"M2"}      # M1 released
    assert [r["market_id"] for r in lib.attempt_ledger(sid)] == ["M1"]


def test_discovery_markets_are_never_valid_evidence(lib):
    """A live market's lastTs keeps growing, so a discovery market can drift
    into a later pass's newest-30% holdout. The permanent exclusion list is
    what stops a rule being 'validated' on data that suggested it."""
    sid = lib.upsert_candidate("sigA", RULE_V1, "r",
                               discovery_markets={"M1", "M2"})
    assert lib.excluded_markets(sid) == {"M1", "M2"}
    lib.record_validation(sid, "M3", trades=10, wins=6, pnl=1.0, drawdown=0.2)
    assert lib.excluded_markets(sid) == {"M1", "M2", "M3"}
    # Re-discovery widens the exclusion; it never narrows it.
    lib.upsert_candidate("sigA", RULE_V1, "r", discovery_markets={"M4"})
    assert lib.excluded_markets(sid) == {"M1", "M2", "M3", "M4"}


def test_pre_exclusion_libraries_migrate_in_place(tmp_path):
    """Libraries created before the exclusion column must open and work."""
    import sqlite3
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE strategies (id TEXT PRIMARY KEY, signature TEXT, "
        "version INTEGER, rule TEXT, rule_hash TEXT, describe TEXT, "
        "status TEXT DEFAULT 'new', created_ts REAL, updated_ts REAL, "
        "last_validated_ts REAL DEFAULT 0, retired_reason TEXT DEFAULT '', "
        "in_score REAL DEFAULT 0, in_win REAL DEFAULT 0, "
        "in_sharpe REAL DEFAULT 0)")
    conn.commit()
    conn.close()
    lib = StrategyLibrary(path)
    try:
        sid = lib.upsert_candidate("s", RULE_V1, "r",
                                   discovery_markets={"M1"})
        assert lib.excluded_markets(sid) == {"M1"}
    finally:
        lib.close()


# -- the gradual status ladder -------------------------------------------------

def _cum(trades: int, markets: int, expectancy: float,
         wins: int | None = None) -> dict:
    return {"trades": trades, "markets": markets, "expectancy": expectancy,
            "wins": wins if wins is not None else trades // 2,
            "pnl": expectancy * trades, "drawdown": 1.0, "win_rate": 0.5,
            "period": ""}


GOOD_PASS = {"trades": 20, "wins": 12, "pnl": 3.0}
BAD_PASS = {"trades": 20, "wins": 5, "pnl": -4.0}


def test_no_evidence_stays_new():
    assert next_status("new", _cum(0, 0, 0.0), None, _cfg())[0] == "new"


def test_thin_evidence_is_validating():
    assert next_status("new", _cum(5, 1, 0.9), GOOD_PASS, _cfg())[0] \
        == "validating"


def test_enough_evidence_validates():
    assert next_status("validating", _cum(40, 4, 0.5), GOOD_PASS, _cfg())[0] \
        == "validated"


def test_decisively_negative_first_showing_is_rejected():
    status, reason = next_status("validating", _cum(40, 4, -0.3), BAD_PASS,
                                 _cfg())
    assert status == "rejected" and "negative expectancy" in reason


def test_deep_evidence_reaches_high_confidence():
    assert next_status("validated", _cum(150, 6, 0.4), GOOD_PASS, _cfg())[0] \
        == "high_confidence"


def test_one_bad_pass_demotes_validated_to_watch_not_bin():
    status, _ = next_status("validated", _cum(80, 5, 0.2), BAD_PASS, _cfg())
    assert status == "watch"


def test_sustained_deterioration_walks_the_whole_ladder():
    cum = _cum(120, 6, 0.05)
    path = []
    status = "validated"
    for _ in range(3):
        status, _ = next_status(status, cum, BAD_PASS, _cfg())
        path.append(status)
    assert path == ["watch", "degraded", "retired"]


def test_recovery_is_also_gradual():
    cum = _cum(120, 6, 0.3)
    assert next_status("degraded", cum, GOOD_PASS, _cfg())[0] == "watch"
    assert next_status("watch", cum, GOOD_PASS, _cfg())[0] == "validated"


def test_retired_is_terminal_even_with_a_great_record():
    great = _cum(500, 20, 1.0)
    assert next_status("retired", great, GOOD_PASS, _cfg())[0] == "retired"


def test_rejected_returns_only_when_the_whole_record_flips():
    still_bad = _cum(60, 6, -0.02)
    assert next_status("rejected", still_bad, GOOD_PASS, _cfg())[0] \
        == "rejected"
    # Enough NEW unseen evidence to flip the CUMULATIVE record positive:
    # back in at validating — one step, never straight to trading.
    flipped = _cum(120, 8, 0.03)
    status, reason = next_status("rejected", flipped, GOOD_PASS, _cfg())
    assert status == "validating" and "re-entering validation" in reason


def test_cumulative_expectancy_at_floor_degrades():
    status, reason = next_status("validated", _cum(80, 5, 0.0), GOOD_PASS,
                                 _cfg())
    assert status == "degraded" and "floor" in reason


def test_retired_rows_are_kept_forever(lib):
    sid = lib.upsert_candidate("sigA", RULE_V1, "r")
    lib.set_status(sid, "retired", "sustained deterioration")
    assert lib.evaluable() == []           # never re-challenged
    rows = lib.all_strategies()            # ...but never deleted either
    assert rows and rows[0]["status"] == "retired"
    assert rows[0]["retired_reason"] == "sustained deterioration"


# -- the additive pass, end to end against the real research flow --------------

def test_research_view_reflects_library_not_last_pass(lib):
    """Simulate two passes: pass 2 finds NOTHING new, yet the validated
    strategy from pass 1 must still be on the board. (The pre-library bug:
    every pass overwrote strategies.json.)"""
    sid = lib.upsert_candidate("sigA", RULE_V1, "impulse long")
    for i in range(4):
        lib.record_validation(sid, f"M{i}", trades=12, wins=8, pnl=2.0,
                              drawdown=0.3)
    lib.record_pass(sid, 48, 32, 8.0)
    status, _ = next_status("new", lib.cumulative(sid),
                            lib.recent_passes(sid, 1)[0], _cfg())
    lib.set_status(sid, status)
    # Pass 2: no new candidates, no new holdout markets. The record stands.
    survivors = [s for s in lib.all_strategies()
                 if s["status"] in ("validated", "high_confidence")]
    assert len(survivors) == 1
    assert lib.cumulative(sid)["trades"] == 48
