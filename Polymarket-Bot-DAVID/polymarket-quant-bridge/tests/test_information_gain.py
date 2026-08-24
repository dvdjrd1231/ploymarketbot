"""Choosing WHICH eligible market to spend next.

Eligibility answers "may we use this market". It does not follow that every
permitted market is equally worth using: a candidate whose whole record sits
in one category and one month has not been shown to generalise, and the market
that could show it is one from somewhere else. §7.

The constraint that makes this safe is that variety may only reorder markets
WITHIN a walk-forward class, never across one. Forward position is a property
the validation engine relies on; variety is a research preference. Trading the
first for the second would be this layer reaching into the ladder's business.
"""

from __future__ import annotations

import time

from pqb.eligibility import (FORWARD, HISTORICAL, TRAIT_DIMENSIONS,
                             MarketEligibilityService, MarketRecord,
                             diversity_of, records_from_entries)
from pqb.library import StrategyLibrary

DAY = 86_400.0
DISCOVERED = 1_700_000_000.0


def _lib(tmp_path) -> StrategyLibrary:
    return StrategyLibrary(tmp_path / "library.sqlite3")


def _candidate(library, created_ts: float = DISCOVERED) -> dict:
    sid = library.upsert_candidate("sig", {"type": "sequence"}, "r")
    row = next(r for r in library.all_strategies() if r["id"] == sid)
    row["created_ts"] = created_ts
    return row


def _market(tmp_path, mid: str, category: str, first: float, last: float,
            rows: int = 500) -> MarketRecord:
    path = tmp_path / f"{mid}.csv"
    path.write_text("timestamp,price\n", encoding="utf-8")
    return MarketRecord(market_id=mid, token_id=f"tok{mid}", csv=path,
                        first_ts=first, last_ts=last, rows=rows,
                        category=category)


# -- traits -------------------------------------------------------------------

def test_traits_are_coarse_enough_to_group_and_fine_enough_to_separate(
        tmp_path):
    sports = _market(tmp_path, "A", "Sports", DISCOVERED,
                     DISCOVERED + 10 * DAY, rows=1200)
    assert sports.traits()["category"] == "sports"
    assert sports.traits()["depth"] == "deep"
    assert sports.era == time.strftime(
        "%Y-%m", time.gmtime(DISCOVERED + 10 * DAY))


def test_a_market_with_no_metadata_is_unknown_rather_than_its_own_category(
        tmp_path):
    """Otherwise every undescribed market would look like a cluster of
    identical environments and suppress each other."""
    blank = _market(tmp_path, "B", "", DISCOVERED, DISCOVERED, rows=0)
    assert blank.traits() == {"category": "", "era": blank.era, "depth": ""}


# -- ordering -----------------------------------------------------------------

def test_an_unexplored_environment_is_preferred_over_a_familiar_one(tmp_path):
    """The candidate has only ever been tested in Sports. Of two otherwise
    equal forward markets, the Politics one can tell us something the Sports
    one cannot."""
    lib = _lib(tmp_path)
    row = _candidate(lib)
    lib.record_validation(row["id"], "SEEN", trades=10, wins=6, pnl=1.0,
                          drawdown=0.1)

    markets = [
        _market(tmp_path, "SEEN", "Sports", DISCOVERED - 40 * DAY,
                DISCOVERED - 30 * DAY),
        # Same depth band as the rival so only CATEGORY separates them, and
        # deliberately the larger series, so it would win on the old
        # size-only tiebreak.
        _market(tmp_path, "MORE_SPORTS", "Sports", DISCOVERED + DAY,
                DISCOVERED + 2 * DAY, rows=790),
        _market(tmp_path, "POLITICS", "Politics", DISCOVERED + DAY,
                DISCOVERED + 2 * DAY, rows=600),
    ]
    verdict = MarketEligibilityService(markets, lib).for_candidate(row)
    assert [m.market_id for m in verdict.markets][0] == "POLITICS"
    assert verdict.gains["POLITICS"] > verdict.gains["MORE_SPORTS"]


def test_variety_never_outranks_walk_forward_position(tmp_path):
    """The load-bearing constraint. A historical market from an unexplored
    category is still historical, and forward evidence is the only kind that
    can satisfy a forward-validation requirement."""
    lib = _lib(tmp_path)
    row = _candidate(lib)
    lib.record_validation(row["id"], "SEEN", trades=10, wins=6, pnl=1.0,
                          drawdown=0.1)

    markets = [
        _market(tmp_path, "SEEN", "Sports", DISCOVERED - 40 * DAY,
                DISCOVERED - 30 * DAY),
        _market(tmp_path, "EXOTIC_OLD", "Politics", DISCOVERED - 90 * DAY,
                DISCOVERED - 80 * DAY),
        _market(tmp_path, "SAME_NEW", "Sports", DISCOVERED + DAY,
                DISCOVERED + 2 * DAY),
    ]
    verdict = MarketEligibilityService(markets, lib).for_candidate(row)
    ordered = [m.market_id for m in verdict.markets]
    assert ordered[0] == "SAME_NEW"          # forward beats exotic-historical
    assert verdict.classes["SAME_NEW"] == FORWARD
    assert verdict.classes["EXOTIC_OLD"] == HISTORICAL


def test_size_still_breaks_ties_between_equally_informative_markets(tmp_path):
    lib = _lib(tmp_path)
    row = _candidate(lib)
    markets = [_market(tmp_path, "SMALL", "Sports", DISCOVERED + DAY,
                       DISCOVERED + 2 * DAY, rows=300),
               _market(tmp_path, "BIG", "Sports", DISCOVERED + DAY,
                       DISCOVERED + 2 * DAY, rows=1500)]
    verdict = MarketEligibilityService(markets, lib).for_candidate(row)
    # Same category and era; BIG is 'deep' where SMALL is 'medium', so it is
    # also strictly more informative here — and larger. Either way it leads.
    assert [m.market_id for m in verdict.markets][0] == "BIG"


# -- diversity accounting -----------------------------------------------------

def test_diversity_reports_what_the_record_actually_spans(tmp_path):
    lib = _lib(tmp_path)
    row = _candidate(lib)
    for mid, category in (("A", "Sports"), ("B", "Politics"), ("C", "Sports")):
        lib.record_validation(row["id"], mid, trades=10, wins=6, pnl=1.0,
                              drawdown=0.1, temporal_class=FORWARD)
    markets = [_market(tmp_path, "A", "Sports", DISCOVERED, DISCOVERED),
               _market(tmp_path, "B", "Politics", DISCOVERED, DISCOVERED),
               _market(tmp_path, "C", "Sports", DISCOVERED, DISCOVERED)]
    service = MarketEligibilityService(markets, lib)

    spread = diversity_of(lib, row["id"], lib.cumulative(row["id"]), service)
    assert spread["categories"] == 2
    assert spread["temporal_classes"] == 1


def test_an_unknown_market_makes_a_candidate_look_less_covered_not_more(
        tmp_path):
    """A market that testified and has since been pruned from the cache is
    coverage we cannot see. Being wrong toward 'test it somewhere else'
    risks a redundant replay; being wrong the other way risks concluding
    generalisation from a gap in the index."""
    lib = _lib(tmp_path)
    row = _candidate(lib)
    lib.record_validation(row["id"], "PRUNED", trades=10, wins=6, pnl=1.0,
                          drawdown=0.1)
    service = MarketEligibilityService([], lib)
    spread = diversity_of(lib, row["id"], lib.cumulative(row["id"]), service)
    assert spread["categories"] == 0


def test_the_pool_census_exposes_the_ceiling_on_any_candidates_diversity(
        tmp_path):
    """When the pool holds one category, no candidate can be faulted for
    failing to generalise across categories — and that is a finding about the
    DATA, which the number has to make visible."""
    lib = _lib(tmp_path)
    markets = [_market(tmp_path, m, "Sports", DISCOVERED, DISCOVERED)
               for m in ("A", "B", "C")]
    census = MarketEligibilityService(markets, lib).census()
    assert census["eligibilityPoolEnvironments"]["category"] == 1
    assert set(census["eligibilityPoolEnvironments"]) == set(TRAIT_DIMENSIONS)


def test_category_survives_the_adapter_from_the_research_pass(tmp_path):
    """It was on the export manifest all along and simply never travelled
    this far, which is why the ordering had nothing to order on."""
    records = records_from_entries([
        {"market": "M1", "token": "t", "csv": str(tmp_path / "a.csv"),
         "firstTs": 1.0, "lastTs": 2.0, "rows": 300, "category": "Crypto"}])
    assert records[0].category == "Crypto"
