"""The hypothesis-family evidence layer: the operator's critical tests.

Fragmentation was the bottleneck: versions with 1-7 OOS trades treated as
final answers while the same hypothesis re-registered under new versions.
Pinned here, mapped to the spec's own success tests: family evidence
accumulates across versions with one contribution per independent market
(TEST 1, 2), thresholds make versions not families (TEST 3), 1 OOS trade
is INSUFFICIENT_EVIDENCE not failure (TEST 5), a big negative family
stays unvalidated (TEST 6), and nothing about the trading gate moved
(TEST 10).
"""

from __future__ import annotations

import pytest

from pqb.config import ResearchConfig
from pqb.library import (StrategyLibrary, blocking_of, maturity_of,
                         next_status)

RULE_V1 = {"direction": "long", "entry_feature": "price_z", "entry_op": "<",
           "entry_threshold": -1.0}
RULE_V2 = {**RULE_V1, "entry_threshold": -1.5}


@pytest.fixture()
def lib(tmp_path):
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    yield library
    library.close()


def _cfg() -> ResearchConfig:
    return ResearchConfig()


# -- TEST 1 + 2: family accumulation with strict event dedup -----------------

def test_same_hypothesis_two_markets_two_family_events(lib):
    v1 = lib.upsert_candidate("sigA", RULE_V1, "r")
    lib.record_validation(v1, "MarketA", trades=10, wins=6, pnl=2.0,
                          drawdown=0.2)
    lib.record_validation(v1, "MarketB", trades=8, wins=5, pnl=1.0,
                          drawdown=0.2)
    family = lib.family_cumulative("sigA")
    assert family["markets"] == 2
    assert family["trades"] == 18


def test_one_market_seen_by_multiple_versions_counts_once(lib):
    """TEST 2: three versions detecting one event is ONE observation."""
    v1 = lib.upsert_candidate("sigA", RULE_V1, "r")
    v2 = lib.upsert_candidate("sigA", RULE_V2, "r")
    lib.record_validation(v1, "MarketB", trades=10, wins=6, pnl=2.0,
                          drawdown=0.2)
    lib.record_validation(v2, "MarketB", trades=12, wins=2, pnl=-3.0,
                          drawdown=0.5)
    lib.record_validation(v2, "MarketC", trades=5, wins=3, pnl=1.0,
                          drawdown=0.1)
    family = lib.family_cumulative("sigA")
    assert family["versions"] == 2
    assert family["markets"] == 2                # B once + C once, never B twice
    # Market B contributes its FIRST-recorded testimony (chronological,
    # no picking the flattering version).
    assert family["trades"] == 10 + 5
    # Version ledgers stay untouched and independent.
    assert lib.cumulative(v1)["trades"] == 10
    assert lib.cumulative(v2)["trades"] == 17


# -- TEST 3: thresholds = versions, not families -----------------------------

def test_new_threshold_same_family_separate_version(lib):
    v1 = lib.upsert_candidate("sigA", RULE_V1, "r")
    v2 = lib.upsert_candidate("sigA", RULE_V2, "r")
    assert v1 != v2
    assert v1.split("#")[0] == v2.split("#")[0]     # one hypothesis family
    assert lib.family_cumulative("sigA")["versions"] == 2


def test_different_hypothesis_is_a_different_family():
    from pqb.research import signature_of

    after_liquidity = {"direction": "long", "entry_feature": "liq_imbalance",
                       "entry_op": "<"}
    after_momentum = {"direction": "long", "entry_feature": "px_velocity_5",
                      "entry_op": "<"}
    assert signature_of(after_liquidity) != signature_of(after_momentum)


# -- TEST 5 + §8: insufficient evidence vs failure ---------------------------

def _cum(trades, markets=1, expectancy=0.1, top_share=0.3):
    return {"trades": trades, "markets": markets, "expectancy": expectancy,
            "wins": max(0, trades // 2), "pnl": expectancy * trades,
            "drawdown": 1.0, "win_rate": 0.5, "period": "",
            "top_share": top_share}


def test_one_oos_trade_is_insufficient_evidence_not_failure():
    cfg = _cfg()
    for trades in (1, 3, 7):
        cumulative = _cum(trades, expectancy=-0.5)      # even losing ones
        status, _ = next_status("validating", cumulative, None, cfg)
        assert status == "validating"                   # gate untouched
        assert maturity_of(status, cumulative, cfg) == \
            "INSUFFICIENT_EVIDENCE"


def test_maturity_ladder_states():
    cfg = _cfg()
    assert maturity_of("new", _cum(0), cfg) == "DISCOVERED"
    # Mid-sample with a POSITIVE record is a NEAR_MISS (operator's §24);
    # the same sample losing money is plain accumulation.
    assert maturity_of("validating", _cum(15), cfg) == "NEAR_MISS"
    assert maturity_of("validating", _cum(15, expectancy=-0.1), cfg) == \
        "EVIDENCE_ACCUMULATING"
    assert maturity_of("validating", _cum(35, markets=2), cfg) == \
        "SUFFICIENT_SAMPLE_FOR_REVIEW"
    assert maturity_of("validated", _cum(50, markets=4), cfg) == "VALIDATED"
    assert maturity_of("rejected", _cum(50), cfg) == "REJECTED"


# -- §18: why-not-validated diagnostics --------------------------------------

def test_blocking_names_the_condition():
    cfg = _cfg()
    assert "INSUFFICIENT_OOS_EVENTS" in \
        blocking_of("validating", _cum(5), cfg)
    assert "INSUFFICIENT_MARKETS" in \
        blocking_of("validating", _cum(40, markets=1), cfg)
    assert "NEGATIVE_NET_EXPECTANCY" in \
        blocking_of("validating", _cum(40, markets=4, expectancy=-0.1), cfg)
    assert "EVENT_CONCENTRATION" in \
        blocking_of("validating", _cum(40, markets=4, top_share=0.95), cfg)
    assert blocking_of("validated", _cum(40, markets=4), cfg) == ""


# -- OOS breadth (operator's addendum): symmetric for both directions --------

def test_single_market_losses_wait_instead_of_rejecting():
    """One market's 30 losing trades is one market's story: the candidate
    stays validating and waits for unseen markets — rejection needs
    breadth, exactly as promotion does."""
    cfg = _cfg()
    one_market = _cum(35, markets=1, expectancy=-0.3)
    status, _ = next_status("validating", one_market, None, cfg)
    assert status == "validating"
    assert "INSUFFICIENT_MARKETS" in blocking_of(status, one_market, cfg)
    # The same record across TWO independent markets is real failure.
    two_markets = _cum(35, markets=2, expectancy=-0.3)
    status, reason = next_status("validating", two_markets, None, cfg)
    assert status == "rejected"
    assert "2 markets" in reason


def test_single_market_wins_cannot_validate_either():
    """Breadth symmetry's other half — already law, re-pinned here."""
    cfg = _cfg()
    status, _ = next_status("validating",
                            _cum(100, markets=1, expectancy=0.5), None, cfg)
    assert status == "validating"


def test_breadth_starvation_triggers_market_search(tmp_path):
    """When candidates queue on INSUFFICIENT_MARKETS and the holdout pool
    is thin, the correct move is expanding the eligible-market search."""
    import types

    from pqb.config import Config
    from pqb.logs import Log
    from pqb.runner import Runner

    cfg = Config()
    cfg.root = tmp_path
    runner = Runner(cfg, Log())
    runner.intel_store = types.SimpleNamespace()
    floor = runner.config.research.auto_backfill_min_series
    # Enough series exported, but candidates blocked on breadth: refill.
    assert runner._needs_series_refill(exported=floor + 2,
                                       breadth_blocked=3,
                                       holdout_series=2) is True
    # Plenty of holdout markets already: no refill needed for breadth.
    assert runner._needs_series_refill(exported=floor + 2,
                                       breadth_blocked=3,
                                       holdout_series=10) is False


# -- TEST 6: sample size alone never validates -------------------------------

def test_big_negative_family_stays_unvalidated(lib):
    sid = lib.upsert_candidate("sigA", RULE_V1, "r")
    for i in range(10):
        lib.record_validation(sid, f"M{i}", trades=10, wins=3, pnl=-2.0,
                              drawdown=0.5)
    cumulative = lib.cumulative(sid)
    assert cumulative["trades"] == 100
    status, _ = next_status("validating", cumulative, None, _cfg())
    assert status in ("rejected", "degraded")       # never validated


# -- TEST 10 + §19: the trading gate did not move ----------------------------

def test_family_evidence_never_feeds_the_trading_gate(tmp_path):
    """A version with a huge FAMILY ledger but thin own evidence must not
    trade: tradable still keys on the version's own status alone."""
    from pqb.research import DiscoveredStrategy

    strategy = DiscoveredStrategy(rule=dict(RULE_V1), signature="sigA#v9",
                                  describe="r")
    strategy.status = "validating"
    strategy.family_trades = 5_000
    strategy.family_markets = 60
    strategy.family_expectancy = 1.0
    assert strategy.tradable is False


def test_view_serialization_roundtrip():
    from pqb.research import DiscoveredStrategy

    strategy = DiscoveredStrategy(rule={}, signature="s", describe="d")
    strategy.maturity = "INSUFFICIENT_EVIDENCE"
    strategy.blocking = "INSUFFICIENT_OOS_EVENTS (3/30)"
    strategy.family_trades = 87
    strategy.family_markets = 31
    data = strategy.to_dict()
    back = DiscoveredStrategy.from_dict(data)
    assert back.maturity == "INSUFFICIENT_EVIDENCE"
    assert back.family_trades == 87
    assert back.family_markets == 31
