"""Bidirectional + hold discovery: direction and duration are variables.

The operator's acceptance criteria, pinned: inverse variants spawn only
from decisive negative evidence and inherit the parent's discovery
exclusions but ZERO validation (his #4); hold variants spawn only from
promise (his own expand-on-evidence rule); NO-TRADE stays the default for
everything unproven; bridge rules are exempt (their search already covers
both directions and hold ladders); and the longshot high-side replay is
the true complement of the low side.
"""

from __future__ import annotations

import pytest

from pqb.config import ResearchConfig
from pqb.library import StrategyLibrary
from pqb.research import signature_of, variant_expansions


def _cfg() -> ResearchConfig:
    return ResearchConfig()


def _cum(trades, expectancy, markets=3):
    return {"trades": trades, "markets": markets, "expectancy": expectancy,
            "wins": trades // 2, "pnl": expectancy * trades,
            "drawdown": 1.0, "win_rate": 0.5, "period": "",
            "top_share": 0.3}


SEQ_RULE = {"type": "sequence", "chain": ["a", "b"], "direction": "up",
            "gap_bars": 15, "hold_bars": 15}


@pytest.fixture()
def lib(tmp_path):
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    yield library
    library.close()


def _entry(lib, rule, describe="r", discovery=()):
    sid = lib.upsert_candidate(signature_of(rule), rule, describe,
                               discovery_markets=set(discovery))
    return next(s for s in lib.all_strategies() if s["id"] == sid)


# -- inverse variants --------------------------------------------------------

def test_decisive_loser_spawns_its_inverse(lib):
    entry = _entry(lib, SEQ_RULE)
    variants = variant_expansions(lib, entry, _cum(20, -0.10), _cfg())
    inverses = [(r, d) for r, d in variants if r.get("variant") == "inverse"]
    assert len(inverses) == 1
    rule, describe = inverses[0]
    assert rule["direction"] == "down"           # flipped
    assert rule["variant_of"] == entry["id"]
    assert describe.startswith("INVERSE")


def test_thin_or_mild_losses_spawn_nothing(lib):
    entry = _entry(lib, SEQ_RULE)
    assert variant_expansions(lib, entry, _cum(5, -0.50), _cfg()) == []
    mild = variant_expansions(lib, entry, _cum(20, -0.005), _cfg())
    assert not any(r.get("variant") == "inverse" for r, _ in mild)


def test_inverse_not_duplicated_once_registered(lib):
    entry = _entry(lib, SEQ_RULE)
    flipped = dict(SEQ_RULE, direction="down")
    lib.upsert_candidate(signature_of(flipped), flipped, "already there")
    variants = variant_expansions(lib, entry, _cum(20, -0.10), _cfg())
    assert not any(r.get("variant") == "inverse" for r, _ in variants)


def test_inverse_starts_with_zero_inherited_evidence(lib):
    """Acceptance #4: side selection must not inherit validation."""
    entry = _entry(lib, SEQ_RULE, discovery={"A", "B"})
    lib.record_validation(entry["id"], "M1", trades=20, wins=4, pnl=-2.0,
                          drawdown=1.0)
    (rule, describe), = [v for v in variant_expansions(
        lib, entry, _cum(20, -0.10), _cfg())
        if v[0].get("variant") == "inverse"]
    vid = lib.upsert_candidate(signature_of(rule), rule, describe,
                               discovery_markets={"A", "B"})
    assert lib.cumulative(vid)["trades"] == 0            # zero inheritance
    assert lib.excluded_markets(vid) == {"A", "B"}       # leakage guard kept


def test_longshot_inverse_flips_side_not_direction(lib):
    shot = {"type": "longshot", "category": "military", "prob_lo": 0.15,
            "prob_hi": 0.25, "side": "low", "min_traded_usd": 0.0}
    entry = _entry(lib, shot)
    variants = variant_expansions(lib, entry, _cum(15, -0.10), _cfg())
    (rule, _), = [v for v in variants if v[0].get("variant") == "inverse"]
    assert rule["side"] == "high"


# -- hold variants -----------------------------------------------------------

def test_promise_spawns_half_and_double_holds(lib):
    entry = _entry(lib, SEQ_RULE)
    variants = variant_expansions(lib, entry, _cum(15, +0.05), _cfg())
    holds = sorted(r["hold_bars"] for r, _ in variants
                   if "hold" in str(r.get("variant")))
    assert holds == [7, 30]                       # half and double of 15
    for rule, _ in variants:
        assert signature_of(rule) == signature_of(SEQ_RULE)   # same family


def test_losers_never_spawn_hold_ladders(lib):
    entry = _entry(lib, SEQ_RULE)
    variants = variant_expansions(lib, entry, _cum(15, -0.10), _cfg())
    assert not any("hold" in str(r.get("variant")) for r, _ in variants)


def test_hold_variants_spawn_once_per_family(lib):
    entry = _entry(lib, SEQ_RULE)
    second = dict(SEQ_RULE, hold_bars=7)
    lib.upsert_candidate(signature_of(second), second, "v2 exists")
    variants = variant_expansions(lib, entry, _cum(15, +0.05), _cfg())
    assert not any("hold" in str(r.get("variant")) for r, _ in variants)


# -- exemptions and defaults -------------------------------------------------

def test_bridge_rules_are_exempt(lib):
    bridge_rule = {"direction": "long", "entry_feature": "price_z",
                   "entry_op": "<"}
    entry = _entry(lib, bridge_rule)
    assert variant_expansions(lib, entry, _cum(50, -0.50), _cfg()) == []


def test_no_trade_stays_the_default(tmp_path):
    """His #3: NO TRADE is the ladder's answer for everything unproven —
    variants included. A freshly spawned inverse cannot trade."""
    from pqb.research import DiscoveredStrategy

    inverse = DiscoveredStrategy(
        rule=dict(SEQ_RULE, direction="down", variant="inverse"),
        signature="seq|a|b|down", describe="INVERSE")
    inverse.status = "new"
    assert inverse.tradable is False


# -- longshot high side: the true complement ---------------------------------

def test_longshot_high_side_replay_is_complementary():
    from pqb.analytics.longshot import frozen_replay

    tape = [{"ts": 1_000 + i * 60, "price": 0.20, "usdc": 100.0}
            for i in range(40)]
    low = {"type": "longshot", "prob_lo": 0.15, "prob_hi": 0.25,
           "side": "low", "min_traded_usd": 0.0}
    high = dict(low, side="high")
    low_stats = frozen_replay(tape, low, payout=1.0, cost=0.02)
    high_stats = frozen_replay(tape, high, payout=1.0, cost=0.02)
    assert low_stats["pnl"] == pytest.approx(1.0 - 0.20 - 0.02)
    assert high_stats["pnl"] == pytest.approx(0.0 - 0.80 - 0.02)
    # Gross outcomes are exact complements; both pay the same cost.
    assert low_stats["pnl"] + high_stats["pnl"] == pytest.approx(-0.04)
