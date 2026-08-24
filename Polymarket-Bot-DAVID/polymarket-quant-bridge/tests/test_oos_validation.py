"""TRUE out-of-sample validation (the operator's spec, made mechanical).

The one forbidden thing: proving a strategy on the data that discovered it.
These tests pin the split (market-level, newest held out), the freeze, the
sample-aware confidence, the status ladder, and the only-validated-may-trade
rule in the engine.
"""

from __future__ import annotations

import types

import pytest

from pqb.config import Config, ResearchConfig
from pqb.research import (DiscoveredStrategy, assign_status,
                          split_markets_by_date, wilson_lower_bound)


# -- sample-aware confidence ---------------------------------------------------

def test_wilson_makes_small_samples_humble():
    """98% of 8 trades must NOT read like 98% of 500."""
    tiny = wilson_lower_bound(wins=8, n=8)
    big = wilson_lower_bound(wins=490, n=500)
    assert tiny < 0.75
    assert big > 0.95
    assert big > tiny


def test_wilson_edge_cases():
    assert wilson_lower_bound(0, 0) == 0.0
    assert wilson_lower_bound(0, 50) == 0.0
    assert 0.0 < wilson_lower_bound(25, 50) < 0.5


# -- the split: market-level, newest held out ----------------------------------

def test_split_holds_out_the_newest_markets():
    entries = [{"marketId": f"M{i}", "tokenId": f"t{i}",
                "lastTs": 1_000_000 + i * 1_000} for i in range(10)]
    discovery, oos = split_markets_by_date(entries, oos_fraction=0.3)
    assert len(oos) == 3
    assert discovery.isdisjoint(oos)
    # The held-out set is strictly the NEWEST — walk-forward by construction.
    assert oos == {"M7", "M8", "M9"}


def test_split_with_one_market_holds_out_nothing():
    entries = [{"marketId": "M0", "tokenId": "t0", "lastTs": 1}]
    discovery, oos = split_markets_by_date(entries, 0.3)
    assert discovery == {"M0"} and oos == set()


# -- the status ladder ---------------------------------------------------------

def _cfg() -> ResearchConfig:
    return ResearchConfig()


def test_no_oos_trades_stays_candidate():
    assert assign_status({"trades": 0, "markets": 0, "expectancy": 0}, _cfg()) \
        == "candidate"


def test_small_sample_is_testing_never_validated():
    """Spectacular but tiny: 8 trades cannot validate anything."""
    status = assign_status({"trades": 8, "markets": 2, "expectancy": 5.0},
                           _cfg())
    assert status == "oos_testing"


def test_enough_evidence_validates():
    status = assign_status({"trades": 40, "markets": 4, "expectancy": 0.8},
                           _cfg())
    assert status == "validated"


def test_negative_expectancy_with_sample_fails():
    status = assign_status({"trades": 60, "markets": 5, "expectancy": -0.2},
                           _cfg())
    assert status == "failed_oos"


def test_deep_evidence_reaches_high_confidence():
    status = assign_status({"trades": 150, "markets": 6, "expectancy": 0.5},
                           _cfg())
    assert status == "high_confidence"


# -- only validated strategies may trade ---------------------------------------

def _strategy(status: str, confidence: float = 0.6,
              oos_markets: int = 4) -> DiscoveredStrategy:
    s = DiscoveredStrategy(rule={"entry_feature": "price"}, signature="s",
                           describe="test rule")
    s.status = status
    s.confidence = confidence
    s.oos_markets = oos_markets
    s.oos_trades = 50
    return s


def test_lean_engine_trades_only_validated(tmp_path):
    from pqb.bridge.lean_engine import LeanDecisionEngine

    cfg = Config()
    cfg.root = tmp_path
    engine = LeanDecisionEngine(cfg.engine, config=cfg)
    engine.strategies = [_strategy("candidate"), _strategy("oos_testing"),
                         _strategy("validated"), _strategy("failed_oos")]
    trading = engine.trading_strategies
    assert len(trading) == 1
    assert trading[0].status == "validated"
    # Learning mode keys off the TRADABLE list, not the display list.
    from test_engine import context
    assert "Learning mode" not in engine._entry_block_reason(
        context(balance=100.0), 0)
    engine.strategies = [_strategy("candidate")]
    assert "Learning mode" in engine._entry_block_reason(None, 0)


def test_weight_is_confidence_and_breadth_not_win_rate():
    from pqb.bridge.lean_engine import LeanDecisionEngine

    lucky = _strategy("validated", confidence=0.30, oos_markets=1)
    proven = _strategy("validated", confidence=0.62, oos_markets=5)
    assert LeanDecisionEngine._weight(proven) > LeanDecisionEngine._weight(lucky)


# -- frozen evaluation against the REAL bridge backtester ----------------------

def test_frozen_run_evaluates_a_rule_without_searching(tmp_path):
    """A frozen rule replayed on unseen data through the bridge's own
    backtester — the mechanical heart of true OOS."""
    pytest.importorskip("pandas")
    from pqb.quant import QuantBridgeNotFound, load as load_bridge
    from pqb.research import (_bridge_overrides, _oos_context, _frozen_run,
                              _write_csv)

    try:
        load_bridge()
    except QuantBridgeNotFound:
        pytest.skip("qc_lean_bridge not reachable")

    # A synthetic unseen series: oscillating price, 300 rows.
    rows = []
    for i in range(300):
        price = 0.5 + 0.05 * ((i % 20) - 10) / 10.0
        rows.append({"ts": 1_700_000_000 + i * 60, "price": price,
                     "bid": price, "ask": price, "mid": price})
    token_dir = tmp_path / "tok"
    token_dir.mkdir()
    _write_csv(token_dir / "features.csv", rows)

    cfg = Config()
    cfg.root = tmp_path
    context = _oos_context(load_bridge(), token_dir, tmp_path / "out", cfg)
    # Rules reference ENGINEERED columns (the frame holds only those) —
    # exactly what real discovered rules do. price_z < 0 fires on half the
    # oscillation by construction.
    rule = {"id": "frozen1", "direction": "long", "entry_feature": "price_z",
            "entry_op": "<", "entry_threshold": 0.0, "stop_pct": 5.0,
            "target_pct": 2.0, "time_exit_bars": 10, "contracts": 1}
    stats = _frozen_run(context, rule, fee=0.01)
    assert stats["trades"] > 0, "frozen rule never fired on data built to fire it"
    assert "expectancy" in stats and "drawdown" in stats
