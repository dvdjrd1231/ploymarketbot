"""The operator's discovery correction: fewer, materially stronger candidates.

Pinned here: strategy FAMILIES stop the library filling with spellings of
one idea; repeatedly-refused families lose research slots; a record whose
profit is one market's story cannot be promoted (concentration gate); and
the composite evidence score cannot be dragged up by a single flattering
metric — win rate least of all.
"""

from __future__ import annotations

import pytest

from pqb.config import ResearchConfig
from pqb.library import StrategyLibrary, evidence_score, next_status
from pqb.research import family_of


def _cfg() -> ResearchConfig:
    return ResearchConfig()


# -- families: the idea, not the spelling ------------------------------------

def test_price_z_variants_are_one_family():
    """short ask_z / short bid_z / short mid_z is one idea, not three."""
    rules = [{"direction": "short", "entry_feature": f, "entry_op": ">"}
             for f in ("ask_z", "bid_z", "mid_z", "price_z")]
    assert len({family_of(r) for r in rules}) == 1
    assert family_of(rules[0]) == "mean-reversion"


def test_intent_splits_momentum_from_mean_reversion():
    buy_dip = {"direction": "long", "entry_feature": "price_z",
               "entry_op": "<"}
    buy_strength = {"direction": "long", "entry_feature": "price_z",
                    "entry_op": ">"}
    assert family_of(buy_dip) == "mean-reversion"
    assert family_of(buy_strength) == "momentum"


def test_special_rule_types_have_their_own_families():
    assert family_of({"type": "sequence", "chain": ["a"]}) == "sequence-event"
    assert family_of({"type": "sharp_move"}) == "crash-recovery"
    assert family_of({"entry_feature": "liq_imbalance",
                      "direction": "long", "entry_op": "<"}) == "liquidation"
    assert family_of({"entry_feature": "np_ask_void",
                      "direction": "short", "entry_op": ">"}) \
        == "book-structure"


def test_family_stats_and_storage(tmp_path):
    lib = StrategyLibrary(tmp_path / "lib.sqlite3")
    try:
        a = lib.upsert_candidate("s1", {"entry_feature": "x"}, "r",
                                 family="momentum")
        lib.upsert_candidate("s2", {"entry_feature": "y"}, "r",
                             family="momentum")
        lib.set_status(a, "rejected")
        stats = lib.family_stats()
        assert stats["momentum"]["rejected"] == 1
        assert stats["momentum"]["new"] == 1
    finally:
        lib.close()


# -- concentration: one market's story is not an edge ------------------------

def _cum(trades=60, markets=4, expectancy=0.3, top_share=0.2, wins=None):
    return {"trades": trades, "markets": markets, "expectancy": expectancy,
            "wins": wins if wins is not None else int(trades * 0.6),
            "pnl": expectancy * trades, "drawdown": 1.0,
            "win_rate": 0.6, "period": "", "top_share": top_share}


GOOD_PASS = {"trades": 20, "wins": 12, "pnl": 3.0}


def test_concentrated_profit_cannot_validate():
    status, reason = next_status("validating", _cum(top_share=0.9),
                                 GOOD_PASS, _cfg())
    assert status == "validating"
    assert "concentrated" in reason


def test_diversified_profit_validates():
    status, _ = next_status("validating", _cum(top_share=0.3),
                            GOOD_PASS, _cfg())
    assert status == "validated"


def test_concentration_gate_does_not_block_demotions():
    """The gate stops promotion, never protects a degrading strategy."""
    bad_pass = {"trades": 20, "wins": 5, "pnl": -4.0}
    status, _ = next_status("validated", _cum(top_share=0.9), bad_pass,
                            _cfg())
    assert status == "watch"


def test_cumulative_reports_top_share(tmp_path):
    lib = StrategyLibrary(tmp_path / "lib.sqlite3")
    try:
        sid = lib.upsert_candidate("s", {"entry_feature": "x"}, "r")
        lib.record_validation(sid, "M1", trades=10, wins=6, pnl=9.0,
                              drawdown=0.1)
        lib.record_validation(sid, "M2", trades=10, wins=6, pnl=1.0,
                              drawdown=0.1)
        cum = lib.cumulative(sid)
        assert cum["top_share"] == pytest.approx(0.9)
    finally:
        lib.close()


# -- the composite evidence score --------------------------------------------

def test_evidence_needs_every_dimension():
    cfg = _cfg()
    strong = evidence_score(_cum(trades=100, markets=5, top_share=0.2), cfg)
    assert strong > 0.3
    # Zero in ANY dimension zeroes the score.
    assert evidence_score(_cum(trades=0), cfg) == 0.0
    assert evidence_score(_cum(expectancy=-0.1), cfg) == 0.0
    assert evidence_score(_cum(top_share=1.0), cfg) == 0.0


def test_win_rate_alone_cannot_inflate_evidence():
    """98% winners over 8 trades in 1 market must score far below a modest
    win rate with deep, broad, diversified evidence."""
    cfg = _cfg()
    flashy = evidence_score(_cum(trades=8, markets=1, wins=8,
                                 top_share=0.9), cfg)
    solid = evidence_score(_cum(trades=200, markets=6, wins=120,
                                top_share=0.25), cfg)
    assert solid > flashy * 5


def test_breadth_beats_one_market_volume():
    """1,000 trades from one market must not outrank 200 across many —
    the operator's independence principle, as arithmetic."""
    cfg = _cfg()
    one_market = evidence_score(_cum(trades=1000, markets=1, wins=600,
                                     top_share=1.0), cfg)
    broad = evidence_score(_cum(trades=200, markets=6, wins=120,
                                top_share=0.2), cfg)
    assert broad > one_market
