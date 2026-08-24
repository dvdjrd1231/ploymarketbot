"""The master pipeline fix: OOS breadth by ALLOCATION, not by luck.

The operator's acceptance tests (§20), pinned. The structural repair: the
evaluation pool is every exported series minus each candidate's OWN
exclusions — an old candidate draws unseen evidence from the full pool,
a new candidate still sees only the holdout, and a market still
testifies exactly once per candidate.
"""

from __future__ import annotations

import pytest

from pqb.config import ResearchConfig
from pqb.library import StrategyLibrary, blockers_of, next_status

RULE = {"direction": "long", "entry_feature": "price_z", "entry_op": "<"}


def _cfg() -> ResearchConfig:
    return ResearchConfig()


@pytest.fixture()
def lib(tmp_path):
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    yield library
    library.close()


# -- §20 A/B/C: trades vs markets accounting ---------------------------------

def test_trades_and_markets_account_separately(lib):
    sid = lib.upsert_candidate("sig", RULE, "r")
    lib.record_validation(sid, "M1", trades=5, wins=3, pnl=1.0, drawdown=0.1)
    lib.record_validation(sid, "M2", trades=5, wins=3, pnl=1.0, drawdown=0.1)
    cum = lib.cumulative(sid)
    assert (cum["trades"], cum["markets"]) == (10, 2)      # A
    # B: more trades from the SAME markets change nothing (testify once).
    lib.record_validation(sid, "M1", trades=10, wins=9, pnl=5.0,
                          drawdown=0.0)
    cum = lib.cumulative(sid)
    assert (cum["trades"], cum["markets"]) == (10, 2)
    # C: five genuinely new markets -> markets = 7.
    for i in range(5):
        lib.record_validation(sid, f"N{i}", trades=2, wins=1, pnl=0.2,
                              drawdown=0.1)
    assert lib.cumulative(sid)["markets"] == 7


# -- the pool unlock: old candidates draw from beyond the holdout ------------

def test_old_candidate_can_use_todays_discovery_markets(lib):
    """A candidate frozen in an earlier pass never trained on today's
    discovery markets — they are legitimate unseen evidence for IT, while
    a candidate registered today (whole discovery set excluded) still
    validates only on the holdout."""
    old = lib.upsert_candidate("old", RULE, "r",
                               discovery_markets={"A", "B"})
    new = lib.upsert_candidate("new", dict(RULE, entry_op=">"), "r",
                               discovery_markets={"A", "B", "C", "D"})
    pool = {"A", "B", "C", "D", "H1"}          # discovery + holdout today
    old_eligible = pool - lib.excluded_markets(old)
    new_eligible = pool - lib.excluded_markets(new)
    assert old_eligible == {"C", "D", "H1"}    # breadth beyond the holdout
    assert new_eligible == {"H1"}              # new: holdout only, as before


def test_used_markets_leave_the_candidates_pool(lib):
    sid = lib.upsert_candidate("sig", RULE, "r", discovery_markets={"A"})
    lib.record_validation(sid, "C", trades=3, wins=2, pnl=0.5, drawdown=0.1)
    pool = {"A", "C", "D"}
    assert pool - lib.excluded_markets(sid) == {"D"}


# -- §20 E: evidence never resets across passes ------------------------------

def test_evidence_survives_reopen_and_new_passes(lib, tmp_path):
    sid = lib.upsert_candidate("sig", RULE, "r")
    lib.record_validation(sid, "M1", trades=10, wins=6, pnl=2.0,
                          drawdown=0.1)
    lib.close()
    reopened = StrategyLibrary(tmp_path / "library.sqlite3")
    try:
        assert reopened.cumulative(sid)["trades"] == 10
        # A later pass ADDS; nothing reinitializes.
        reopened.record_validation(sid, "M2", trades=5, wins=3, pnl=1.0,
                                   drawdown=0.1)
        assert reopened.cumulative(sid)["trades"] == 15
    finally:
        reopened.close()


# -- §20 F/G: version freshness and single-market caution --------------------

def test_new_version_starts_with_fresh_ledger(lib):
    v1 = lib.upsert_candidate("sig", RULE, "r")
    lib.record_validation(v1, "M1", trades=20, wins=15, pnl=5.0,
                          drawdown=0.1)
    v2 = lib.upsert_candidate("sig", dict(RULE, entry_threshold=-2.0), "r")
    assert lib.cumulative(v2)["trades"] == 0


def test_excellent_single_market_remains_validating():
    status, _ = next_status(
        "validating",
        {"trades": 100, "markets": 1, "expectancy": 0.9, "wins": 90,
         "pnl": 90.0, "drawdown": 0.1, "win_rate": 0.9, "period": "",
         "top_share": 1.0}, None, _cfg())
    assert status == "validating"


# -- §9/§17.9: structured blockers with numeric targets ----------------------

def test_blockers_carry_numeric_targets():
    cfg = _cfg()
    cumulative = {"trades": 26, "markets": 3, "expectancy": -0.02,
                  "wins": 10, "pnl": -0.5, "drawdown": 1.0,
                  "win_rate": 0.38, "period": "", "top_share": 0.8}
    blockers = blockers_of("validating", cumulative, cfg)
    joined = " | ".join(blockers)
    assert f"OOS_TRADES 26/{cfg.oos_min_trades}" in joined
    assert "OOS_EXPECTANCY -0.02" in joined
    assert "EVENT_CONCENTRATION 80%" in joined
    # Validated rows carry no blockers; zero-evidence rows say so.
    assert blockers_of("validated", cumulative, cfg) == []
    assert blockers_of("validating", {"trades": 0}, cfg) == \
        ["NO_OOS_EVENTS_YET"]


def test_view_serializes_blockers_and_next_action():
    from pqb.research import DiscoveredStrategy

    strategy = DiscoveredStrategy(rule={}, signature="s", describe="d")
    strategy.blockers = ["OOS_MARKET_BREADTH 3/10", "OOS_TRADES 26/40"]
    strategy.next_action = "QUEUED_FOR_OOS (12 unseen in pool)"
    back = DiscoveredStrategy.from_dict(strategy.to_dict())
    assert back.blockers == strategy.blockers
    assert back.next_action == strategy.next_action
