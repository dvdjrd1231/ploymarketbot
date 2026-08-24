"""A rule that did not fire has not been tested there.

The audit's most expensive finding: 114 of 681 validation rows were zero-trade
replays, and because `excluded_markets` reads the validations table, every one
of them permanently spent a market that had produced no evidence. Some
candidates consumed most of the available supply that way.

These tests pin the correction. An ATTEMPT records that compute was spent.
EVIDENCE is created only when the replay actually produced observations. Only
evidence-bearing markets count toward breadth, and only they are consumed.
"""

from __future__ import annotations

import pytest

from pqb.library import (ATTEMPT_ERROR, ATTEMPT_EVIDENCE, ATTEMPT_NO_TRADES,
                         MAX_NO_TRADE_TRIES, StrategyLibrary, blockers_of)


class _Cfg:
    oos_min_trades = 30
    oos_min_markets = 3
    oos_min_expectancy = 0.0
    oos_max_concentration = 0.7


def _library(tmp_path) -> StrategyLibrary:
    return StrategyLibrary(tmp_path / "library.sqlite3")


def _candidate(lib, name="sig") -> str:
    return lib.upsert_candidate(name, {"type": "threshold", "feature": "x"},
                                "test rule")


def test_zero_trade_replay_does_not_consume_the_market(tmp_path):
    """§3, and requirement 1 of the definition of done."""
    lib = _library(tmp_path)
    sid = _candidate(lib)

    lib.record_attempt(sid, "MKT-A", ATTEMPT_NO_TRADES,
                       reason="rule never fired")

    assert lib.cumulative(sid)["markets"] == 0
    assert "MKT-A" not in lib.evidence_markets(sid)
    # The whole point: still available. One non-observation must not burn it.
    assert "MKT-A" not in lib.excluded_markets(sid)


def test_evidence_consumes_the_market_and_counts_once(tmp_path):
    """Requirement 2: evidence-bearing markets counted exactly once."""
    lib = _library(tmp_path)
    sid = _candidate(lib)

    assert lib.record_validation(sid, "MKT-A", trades=9, wins=5, pnl=1.0,
                                 drawdown=0.1)
    # The same market cannot testify twice, however often it is replayed.
    assert not lib.record_validation(sid, "MKT-A", trades=40, wins=40,
                                     pnl=99.0, drawdown=0.0)

    cumulative = lib.cumulative(sid)
    assert cumulative["markets"] == 1
    assert cumulative["trades"] == 9          # not 49
    assert "MKT-A" in lib.excluded_markets(sid)


def test_a_zero_trade_row_can_never_be_written_as_evidence(tmp_path):
    """The invariant is structural, not a call-site convention."""
    lib = _library(tmp_path)
    sid = _candidate(lib)
    with pytest.raises(ValueError, match="did not fire"):
        lib.record_validation(sid, "MKT-A", trades=0, wins=0, pnl=0.0,
                              drawdown=0.0)


def test_repeated_non_observation_is_bounded_then_parked(tmp_path):
    """Bounded retry: a rule that never fires must not re-buy the same
    nothing every pass — but parking is not evidence and not a burn."""
    lib = _library(tmp_path)
    sid = _candidate(lib)

    for _ in range(MAX_NO_TRADE_TRIES):
        lib.record_attempt(sid, "MKT-A", ATTEMPT_NO_TRADES)

    assert "MKT-A" in lib.parked_markets(sid)
    assert "MKT-A" in lib.excluded_markets(sid)   # skipped, to save compute
    assert "MKT-A" not in lib.evidence_markets(sid)
    assert lib.cumulative(sid)["markets"] == 0    # ...but never counted


def test_oos_trades_and_oos_markets_stay_separate(tmp_path):
    """Requirement 3, and §14: 30 trades in 2 markets is not 30 in 15."""
    lib = _library(tmp_path)
    narrow, broad = _candidate(lib, "narrow"), _candidate(lib, "broad")

    for i in range(2):
        lib.record_validation(narrow, f"N{i}", trades=15, wins=8, pnl=1.0,
                              drawdown=0.1)
    for i in range(15):
        lib.record_validation(broad, f"B{i}", trades=2, wins=1, pnl=0.13,
                              drawdown=0.1)

    assert lib.cumulative(narrow)["trades"] == 30
    assert lib.cumulative(broad)["trades"] == 30
    assert lib.cumulative(narrow)["markets"] == 2
    assert lib.cumulative(broad)["markets"] == 15

    narrow_blocked = blockers_of("validating", lib.cumulative(narrow), _Cfg())
    broad_blocked = blockers_of("validating", lib.cumulative(broad), _Cfg())
    assert any(b.startswith("OOS_MARKET_BREADTH") for b in narrow_blocked)
    assert not any(b.startswith("OOS_MARKET_BREADTH") for b in broad_blocked)


def test_replay_failures_are_visible_not_silent(tmp_path):
    """Requirement 10 and §16: a candidate that crashes on every market must
    not present as merely untested."""
    lib = _library(tmp_path)
    crashed, never_fired, untouched = (_candidate(lib, "crash"),
                                       _candidate(lib, "quiet"),
                                       _candidate(lib, "new"))

    lib.record_attempt(crashed, "MKT-A", ATTEMPT_ERROR, reason="bad column",
                       exc_type="KeyError", stage="replay:threshold")
    lib.record_attempt(never_fired, "MKT-A", ATTEMPT_NO_TRADES)

    summaries = lib.attempt_summaries()
    empty = {"trades": 0, "markets": 0}

    assert blockers_of("new", empty, _Cfg(), summaries.get(crashed)) == \
        ["DATA_FAILURE 1 market(s) errored during replay"]
    assert blockers_of("new", empty, _Cfg(), summaries.get(never_fired)) == \
        ["RULE_NEVER_FIRED in 1 attempted market(s)"]
    assert blockers_of("new", empty, _Cfg(), summaries.get(untouched)) == \
        ["NO_OOS_EVENTS_YET"]

    ledger = lib.attempt_ledger(crashed)
    assert ledger[0]["exc_type"] == "KeyError"
    assert ledger[0]["stage"] == "replay:threshold"


def test_legacy_zero_trade_burns_are_migrated_and_markets_released(tmp_path):
    """§28: preserve the record, release the markets. Nothing is deleted from
    history — the burn becomes an attempt, keeping its original timestamp."""
    path = tmp_path / "library.sqlite3"
    lib = _library(tmp_path)
    sid = _candidate(lib)
    lib.record_validation(sid, "REAL", trades=7, wins=4, pnl=0.5,
                          drawdown=0.1)
    # Write a legacy burn the way the old code did, behind the new guard.
    with lib._lock:                                   # noqa: SLF001
        lib._conn.execute(                            # noqa: SLF001
            "INSERT INTO validations(strategy_id, market_id, ts, trades, "
            "wins, pnl, drawdown) VALUES(?,?,?,0,0,0,0)",
            (sid, "BURNT", 1_700_000_000.0))
        lib._conn.execute("DELETE FROM meta WHERE key=?",   # noqa: SLF001
                          ("zero_trade_burns_migrated",))
        lib._conn.commit()                            # noqa: SLF001
    lib.close()

    reopened = StrategyLibrary(path)          # migration runs on open

    assert "BURNT" not in reopened.excluded_markets(sid)   # released
    assert "REAL" in reopened.excluded_markets(sid)        # untouched
    ledger = {row["market_id"]: row for row in reopened.attempt_ledger(sid)}
    assert ledger["BURNT"]["result"] == ATTEMPT_NO_TRADES
    assert ledger["BURNT"]["first_ts"] == 1_700_000_000.0  # record preserved
    assert reopened.cumulative(sid)["markets"] == 1        # unchanged


def test_attempt_health_reports_the_oos_supply_picture(tmp_path):
    """§23's OOS HEALTH block, from persisted data."""
    lib = _library(tmp_path)
    a, b = _candidate(lib, "a"), _candidate(lib, "b")
    lib.record_attempt(a, "M1", ATTEMPT_EVIDENCE, trades=5)
    lib.record_validation(a, "M1", trades=5, wins=3, pnl=0.2, drawdown=0.1)
    lib.record_attempt(a, "M2", ATTEMPT_NO_TRADES)
    lib.record_attempt(b, "M3", ATTEMPT_ERROR, exc_type="ValueError")

    health = lib.attempt_health()
    assert health["evidenceAttempts"] == 1
    assert health["zeroTradeAttempts"] == 1
    assert health["errorAttempts"] == 1
    assert health["candidates"] == 2
    assert health["candidatesWithZeroOosMarkets"] == 1     # b has none
    assert health["maxOosMarketsPerCandidate"] == 1
    # M2 was attempted once and may be retried; it is not parked yet.
    assert health["retryableMarkets"] == 2
