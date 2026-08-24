"""Research allocation (§7/§23): evidence goes where promise is.

Pinned: priority favors positive OOS records and punishes negative ones
and in-sample/OOS divergence; NEAR_MISS names promising-but-underpowered
candidates; overfit risk only speaks with sample; meta family weights are
bounded steering; and none of it can promote or trade anything.
"""

from __future__ import annotations

import pytest

from pqb.config import ResearchConfig
from pqb.library import (maturity_of, meta_family_weights, overfit_risk,
                         research_priority)


def _cfg() -> ResearchConfig:
    return ResearchConfig()


def _cum(trades, markets=2, expectancy=0.1, top_share=0.2, wins=None):
    return {"trades": trades, "markets": markets, "expectancy": expectancy,
            "wins": wins if wins is not None else trades // 2,
            "pnl": expectancy * trades, "drawdown": 1.0,
            "win_rate": (wins if wins is not None else trades // 2)
            / max(1, trades), "period": "", "top_share": top_share}


# -- priority ordering -------------------------------------------------------

def test_positive_oos_outranks_negative():
    cfg = _cfg()
    promising = research_priority(_cum(15, expectancy=0.2), "validating",
                                  0.6, periods=2, cfg=cfg)
    losing = research_priority(_cum(15, expectancy=-0.2), "validating",
                               0.9, periods=2, cfg=cfg)
    assert promising > losing
    assert losing <= 0.2       # negative unseen records earn few resources


def test_high_in_sample_cannot_buy_priority():
    """The operator's rule: priority must not be won by in-sample beauty."""
    cfg = _cfg()
    flashy = research_priority(_cum(20, expectancy=-0.1, wins=2),
                               "validating", 0.95, periods=1, cfg=cfg)
    modest = research_priority(_cum(20, expectancy=0.1, wins=11),
                               "validating", 0.55, periods=2, cfg=cfg)
    assert modest > flashy


def test_breadth_and_time_diversity_raise_priority():
    cfg = _cfg()
    narrow = research_priority(_cum(20, markets=1, top_share=0.9),
                               "validating", 0.6, periods=1, cfg=cfg)
    broad = research_priority(_cum(20, markets=4, top_share=0.2),
                              "validating", 0.6, periods=3, cfg=cfg)
    assert broad > narrow


def test_terminal_states_get_zero():
    cfg = _cfg()
    assert research_priority(_cum(50), "rejected", 0.6, 2, cfg) == 0.0
    assert research_priority(_cum(50), "retired", 0.6, 2, cfg) == 0.0


# -- overfit risk (§12) ------------------------------------------------------

def test_divergence_is_measured_with_sample():
    assert overfit_risk(0.83, _cum(30, wins=3)) == pytest.approx(0.73)
    # Thin OOS samples never speak.
    assert overfit_risk(0.83, _cum(5, wins=0)) == 0.0


def test_overfit_risk_lowers_priority():
    cfg = _cfg()
    clean = research_priority(_cum(30, expectancy=0.1, wins=17),
                              "validating", 0.60, periods=2, cfg=cfg)
    diverged = research_priority(_cum(30, expectancy=0.1, wins=17),
                                 "validating", 0.99, periods=2, cfg=cfg)
    assert clean > diverged


# -- NEAR_MISS (§24) ---------------------------------------------------------

def test_near_miss_names_promising_underpowered_candidates():
    cfg = _cfg()
    assert maturity_of("validating", _cum(5, expectancy=0.2), cfg) \
        == "NEAR_MISS"
    assert maturity_of("validating", _cum(5, expectancy=-0.2), cfg) \
        == "INSUFFICIENT_EVIDENCE"
    assert maturity_of("validating", _cum(20, expectancy=0.2), cfg) \
        == "NEAR_MISS"


def test_near_miss_is_never_tradable():
    from pqb.research import DiscoveredStrategy

    strategy = DiscoveredStrategy(rule={}, signature="s", describe="d")
    strategy.status = "validating"
    strategy.maturity = "NEAR_MISS"
    strategy.priority = 1.5
    assert strategy.tradable is False


# -- meta weights (§21) ------------------------------------------------------

def test_meta_weights_are_bounded_steering():
    metrics = [
        {"signature": "winner", "trades": 50, "expectancy": 0.2},
        {"signature": "loser", "trades": 50, "expectancy": -0.2},
        {"signature": "unknown", "trades": 3, "expectancy": 0.9},
    ]
    weights = meta_family_weights(metrics)
    assert weights["winner"] == 1.2
    assert weights["loser"] == 0.8
    assert weights["unknown"] == 1.0       # thin evidence steers nothing
    assert all(0.8 <= w <= 1.2 for w in weights.values())


def test_serialization_roundtrip():
    from pqb.research import DiscoveredStrategy

    strategy = DiscoveredStrategy(rule={}, signature="s", describe="d")
    strategy.priority = 0.87
    strategy.overfit_risk = 0.44
    strategy.oos_periods = 3
    back = DiscoveredStrategy.from_dict(strategy.to_dict())
    assert back.priority == pytest.approx(0.87)
    assert back.overfit_risk == pytest.approx(0.44)
    assert back.oos_periods == 3
