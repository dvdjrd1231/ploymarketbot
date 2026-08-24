"""The impulse-anomaly master spec's gaps: impulse columns, the edge exit as
the primary exit, the deviation throttle, and the market-wide regime."""

from __future__ import annotations

import time

from pqb.analytics import regime
from pqb.features import token_features, FEATURE_NAMES
from pqb.models import Action
from test_engine import NOW, context, engine, market, position, decision_for


# -- impulse columns ---------------------------------------------------------

def test_tape_impulse_columns_are_computed():
    m = market()
    m.recent_trades = [
        {"tokenId": "tok1", "side": "BUY", "price": 0.50, "size": 10, "ts": NOW - 120},
        {"tokenId": "tok1", "side": "BUY", "price": 0.52, "size": 10, "ts": NOW - 60},
        {"tokenId": "tok1", "side": "SELL", "price": 0.55, "size": 50, "ts": NOW},
    ]
    row = token_features(m, m.quote("tok1"), now=NOW)
    assert row["tape_velocity"] > 0            # +10% over 2 min -> fast
    assert row["tape_trade_rate"] == 1.5       # 3 prints over 2 minutes
    assert row["tape_size_concentration"] > 1  # the 50-lot dominates
    assert 0 <= row["level_distance"] <= 0.5
    assert "market_age_hours" in row


def test_regime_columns_default_neutral_not_zero():
    row = token_features(market(), market().quote("tok1"), now=NOW)
    assert row["regime_aggressiveness"] == 1.0


# -- the edge exit (primary) -------------------------------------------------

def test_edge_death_exits_before_take_profit():
    """A winner whose conviction has died leaves as edge_gone, not take_profit."""
    decisions = engine(exits__edge_exit_floor=0.99,   # everything is below it
                       exits__edge_exit_grace_seconds=0,
                       exits__take_profit_pct=0.35).evaluate(
        context(positions=[position(entry=0.50, mark=0.70, peak=0.70)],
                markets=[market(bid=0.70, ask=0.71)]))
    verdict = decision_for(decisions, "tok1")
    assert verdict.action is Action.EXIT
    assert verdict.exit_style == "edge_gone"


def test_edge_exit_respects_grace_and_floor():
    decisions = engine(exits__edge_exit_floor=0.0).evaluate(   # disabled
        context(positions=[position(entry=0.50, mark=0.52)],
                markets=[market(bid=0.52, ask=0.53)]))
    assert decision_for(decisions, "tok1").action is Action.HOLD


def test_stop_loss_still_outranks_the_edge_exit():
    decisions = engine(exits__edge_exit_floor=0.99,
                       exits__edge_exit_grace_seconds=0).evaluate(
        context(positions=[position(entry=0.50, mark=0.30)],
                markets=[market(bid=0.30, ask=0.31)]))
    verdict = decision_for(decisions, "tok1")
    assert verdict.exit_style == "stop"        # catastrophic backstop first


# -- regime ------------------------------------------------------------------

def test_regime_healthy_universe_is_neutral_or_slightly_up():
    """Deep + calm earns at most the small 1.1 bonus - never more."""
    out = regime.measure({m.market_id: m for m in [market(f"m{i}",
                          token_id=f"t{i}") for i in range(4)]})
    assert 1.0 <= out["regime_aggressiveness"] <= 1.1
    assert out["regime_breadth"] == 1.0


def test_regime_dampens_a_thin_universe():
    thin = [market(f"m{i}", token_id=f"t{i}", liquidity=500.0)
            for i in range(4)]
    out = regime.measure({m.market_id: m for m in thin})
    assert out["regime_aggressiveness"] < 1.0


def test_regime_scales_the_stake():
    """The same candidate gets a smaller stake in a dampened regime."""
    eng = engine()
    ctx_full = context(markets=[market()])
    ctx_thin = context(markets=[market()])
    ctx_thin.regime = {"regime_aggressiveness": 0.5}
    assert eng._entry_size(0.8, ctx_thin) < eng._entry_size(0.8, ctx_full)


# -- deviation throttle ------------------------------------------------------

def test_losing_book_throttles_lean_sizing():
    from pqb.bridge.lean_engine import LeanDecisionEngine
    from pqb.config import Config

    cfg = Config()
    eng = LeanDecisionEngine(cfg.engine, config=cfg)

    class _Memory:
        active = True
        book_sample = 30
        book_mean_return = -0.08          # worse than the -0.05 threshold

    eng.memory = _Memory()
    assert eng._size_throttle(context(markets=[market()])) == 0.5

    _Memory.book_mean_return = -0.12      # twice the deviation
    assert eng._size_throttle(context(markets=[market()])) == 0.25

    _Memory.book_mean_return = 0.10       # winning: never scales UP
    assert eng._size_throttle(context(markets=[market()])) == 1.0
