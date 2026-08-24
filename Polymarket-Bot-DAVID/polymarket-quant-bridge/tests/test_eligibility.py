"""One authoritative answer to 'which markets may this candidate use now?'

Before the eligibility service the answer was a single set subtraction at the
replay loop, so every other concern — does the file still exist, does the rule's
feature exist in that series, is the market chronologically after the rule was
discovered — was asked somewhere else or not at all.

The walk-forward point is the subtle one. A settled market older than the
candidate is still genuinely unseen evidence: the rule was never fitted to it.
What it is not is FORWARD validation, and the audit found the persistent pool
treating the two as the same thing.
"""

from __future__ import annotations

import csv

from pqb.eligibility import (CONCURRENT, FORWARD, HISTORICAL, R_DISCOVERY,
                             R_FEATURES, R_MISSING, R_PARKED, R_TESTIFIED,
                             MarketEligibilityService, MarketRecord,
                             classify_pool)
from pqb.library import ATTEMPT_NO_TRADES, MAX_NO_TRADE_TRIES, StrategyLibrary

DISCOVERED = 1_700_000_000.0
DAY = 86_400.0


def _csv(tmp_path, name):
    path = tmp_path / f"{name}.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ts", "price"])
        writer.writeheader()
        for i in range(50):
            writer.writerow({"ts": i, "price": 0.4 + i * 0.001})
    return path


def _market(tmp_path, mid, first, last, rows=200):
    return MarketRecord(market_id=mid, token_id=f"tok{mid}",
                        csv=_csv(tmp_path, mid), first_ts=first,
                        last_ts=last, rows=rows, source="pool")


def _lib(tmp_path):
    return StrategyLibrary(tmp_path / "library.sqlite3")


def _candidate(lib, cid="sig", rule=None):
    sid = lib.upsert_candidate(cid, rule or {"type": "threshold",
                                             "entry_feature": "price"}, "r")
    row = next(r for r in lib.all_strategies() if r["id"] == sid)
    row["created_ts"] = DISCOVERED
    return row


# -- temporal classification --------------------------------------------------

def test_a_market_older_than_discovery_is_not_forward_oos(tmp_path):
    """Requirement 5. Unseen, yes. Walk-forward, no."""
    before = _market(tmp_path, "OLD", DISCOVERED - 30 * DAY,
                     DISCOVERED - 20 * DAY)
    after = _market(tmp_path, "NEW", DISCOVERED + 5 * DAY,
                    DISCOVERED + 10 * DAY)
    straddling = _market(tmp_path, "MID", DISCOVERED - 5 * DAY,
                         DISCOVERED + 5 * DAY)

    assert before.temporal_class(DISCOVERED) == HISTORICAL
    assert after.temporal_class(DISCOVERED) == FORWARD
    assert straddling.temporal_class(DISCOVERED) == CONCURRENT


def test_all_unseen_markets_are_eligible_but_only_some_are_forward(tmp_path):
    """Requirement 4. Refusing historical markets outright would delete most
    of a store built from settled history — the opposite of what §2 is for.
    They count as breadth; they never count as forward confirmation."""
    lib = _lib(tmp_path)
    row = _candidate(lib)
    markets = [_market(tmp_path, "OLD", DISCOVERED - 30 * DAY,
                       DISCOVERED - 20 * DAY),
               _market(tmp_path, "NEW", DISCOVERED + 5 * DAY,
                       DISCOVERED + 10 * DAY)]

    verdict = MarketEligibilityService(markets, lib).for_candidate(row)

    assert verdict.market_ids == {"OLD", "NEW"}
    assert verdict.forward_available() == 1
    assert verdict.classes == {"OLD": HISTORICAL, "NEW": FORWARD}


def test_forward_markets_are_offered_first(tmp_path):
    """The replay budget usually runs out mid-candidate. What it spends
    should be the evidence that can satisfy a forward requirement."""
    lib = _lib(tmp_path)
    row = _candidate(lib)
    markets = [_market(tmp_path, "OLD", DISCOVERED - 30 * DAY,
                       DISCOVERED - 20 * DAY),
               _market(tmp_path, "NEW", DISCOVERED + 5 * DAY,
                       DISCOVERED + 10 * DAY)]

    verdict = MarketEligibilityService(markets, lib).for_candidate(row)
    assert [m.market_id for m in verdict.markets] == ["NEW", "OLD"]


def test_temporal_class_is_stored_on_the_evidence_row(tmp_path):
    """Recomputing it later against a moved clock would quietly reclassify
    settled evidence, so the class is persisted with the row."""
    lib = _lib(tmp_path)
    sid = lib.upsert_candidate("sig", {"type": "threshold"}, "r")
    lib.record_validation(sid, "F1", trades=10, wins=6, pnl=1.0, drawdown=0.1,
                          temporal_class=FORWARD)
    lib.record_validation(sid, "H1", trades=10, wins=6, pnl=1.0, drawdown=0.1,
                          temporal_class=HISTORICAL)

    cumulative = lib.cumulative(sid)
    assert cumulative["markets"] == 2            # both are breadth
    assert cumulative["forward_markets"] == 1    # only one is forward
    assert cumulative["forward_trades"] == 10
    assert cumulative["markets_by_temporal_class"] == {FORWARD: 1,
                                                       HISTORICAL: 1}


# -- the other eligibility concerns, all in one place -------------------------

def test_every_refusal_is_named_and_counted(tmp_path):
    lib = _lib(tmp_path)
    row = _candidate(lib)
    lib.upsert_candidate("sig", {"type": "threshold",
                                 "entry_feature": "price"}, "r",
                         discovery_markets={"CONTAMINATED"})
    lib.record_validation(row["id"], "TESTIFIED", trades=3, wins=2, pnl=0.1,
                          drawdown=0.0)
    for _ in range(MAX_NO_TRADE_TRIES):
        lib.record_attempt(row["id"], "PARKED", ATTEMPT_NO_TRADES)

    gone = MarketRecord(market_id="GONE", token_id="t",
                        csv=tmp_path / "not-here.csv", first_ts=1.0,
                        last_ts=2.0, rows=200)
    markets = [_market(tmp_path, "CONTAMINATED", 1.0, 2.0),
               _market(tmp_path, "TESTIFIED", 1.0, 2.0),
               _market(tmp_path, "PARKED", 1.0, 2.0),
               gone,
               _market(tmp_path, "GOOD", DISCOVERED + DAY, DISCOVERED + DAY)]

    verdict = MarketEligibilityService(markets, lib).for_candidate(row)

    assert verdict.market_ids == {"GOOD"}
    assert verdict.rejected == {R_DISCOVERY: 1, R_TESTIFIED: 1,
                                R_PARKED: 1, R_MISSING: 1}


def test_a_rule_whose_features_are_absent_gets_no_markets(tmp_path):
    """The feature-validity domain and the eligibility service agree: there
    is no point allocating a market to a rule that cannot fire there."""
    from pqb.feature_domain import FeatureDomain, FeatureValidity

    lib = _lib(tmp_path)
    row = _candidate(lib, rule={"type": "threshold",
                                "entry_feature": "spread"})
    domain = FeatureDomain(features={
        "price": FeatureValidity("price", oos_available=True),
        "spread": FeatureValidity("spread", historical_available=True,
                                  coverage=1.0, oos_available=False)})

    verdict = MarketEligibilityService(
        [_market(tmp_path, "GOOD", DISCOVERED + DAY, DISCOVERED + DAY)],
        lib, feature_domain=domain).for_candidate(row)

    assert not verdict.markets
    assert verdict.rejected == {R_FEATURES: 1}
    assert "BLOCKED_ON_FEATURES" in verdict.next_action()


def test_next_action_distinguishes_exhausted_from_starved(tmp_path):
    """§24. 'Nothing to test' and 'nothing left that fires' are different
    problems with different fixes, and used to read identically."""
    lib = _lib(tmp_path)
    row = _candidate(lib)
    for _ in range(MAX_NO_TRADE_TRIES):
        lib.record_attempt(row["id"], "M1", ATTEMPT_NO_TRADES)

    exhausted = MarketEligibilityService(
        [_market(tmp_path, "M1", 1.0, 2.0)], lib).for_candidate(row)
    assert "EXHAUSTED" in exhausted.next_action()

    starved = MarketEligibilityService([], lib).for_candidate(row)
    assert "WAITING_FOR_DATA" in starved.next_action()

    queued = MarketEligibilityService(
        [_market(tmp_path, "M2", DISCOVERED + DAY, DISCOVERED + DAY)],
        lib).for_candidate(row)
    assert queued.next_action().startswith("QUEUED_FOR_OOS")
    assert "1 forward" in queued.next_action()


def test_pool_temporal_mix_is_reported_as_a_property_of_the_data(tmp_path):
    """'No forward evidence exists yet' should read as a fact about the
    store, not as a failing of every candidate in the library."""
    markets = [_market(tmp_path, "A", DISCOVERED - 40 * DAY,
                       DISCOVERED - 30 * DAY),
               _market(tmp_path, "B", DISCOVERED - 20 * DAY,
                       DISCOVERED - 10 * DAY),
               _market(tmp_path, "C", DISCOVERED + DAY, DISCOVERED + 2 * DAY)]

    assert classify_pool(markets, DISCOVERED) == {HISTORICAL: 2, FORWARD: 1}
