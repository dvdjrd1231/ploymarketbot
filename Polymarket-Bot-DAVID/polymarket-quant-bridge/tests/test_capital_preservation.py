"""Capital preservation: caps that shrink, never grow, and never trap.

The four properties worth a test each are the four ways an account like this
usually dies — martingale, revenge sizing, leverage escalation, and a risk
control that blocks the exit as well as the entry. All four are absent by
construction here, and these tests are what keeps them absent.
"""

from __future__ import annotations

import pytest

from pqb import riskpolicy
from pqb.config import CapitalPreservationConfig


def _cfg(**overrides):
    cfg = CapitalPreservationConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _pos(token, value, market="", category="", wallet=""):
    return riskpolicy.ExposureView(token_id=token, market_id=market,
                                   category=category, wallet_thesis=wallet,
                                   value=value, cost=value)


# -- 1. disabled changes nothing --------------------------------------------


def test_disabled_blocks_nothing_and_scales_nothing():
    verdict = riskpolicy.evaluate(_cfg(enabled=False), 41.86, 5.0,
                                  [_pos("a", 30.0)], peak_equity=100.0)
    assert not verdict.blocked
    assert verdict.scale == 1.0


# -- 2. the caps -------------------------------------------------------------


def test_the_cash_reserve_blocks_entries_when_cash_runs_out():
    verdict = riskpolicy.evaluate(_cfg(), equity=41.86, cash=2.0,
                                  positions=[_pos("a", 39.0)],
                                  peak_equity=100.0)
    assert verdict.blocked
    assert "reserve" in verdict.block_reason
    assert "Exits are unaffected" in verdict.block_reason


def test_total_exposure_is_capped_as_a_share_of_equity():
    verdict = riskpolicy.evaluate(
        _cfg(min_cash_reserve_fraction=0.0, max_total_exposure_fraction=0.60),
        equity=100.0, cash=40.0,
        positions=[_pos("a", 30.0, market="m1"), _pos("b", 31.0, market="m2")],
        peak_equity=100.0)
    assert verdict.blocked
    assert "ceiling" in verdict.block_reason


def test_four_positions_in_one_market_are_one_bet():
    """The §13 property: correlated exposure binds on the CLUSTER."""
    positions = [_pos(f"t{i}", 8.0, market="same") for i in range(4)]
    verdict = riskpolicy.evaluate(
        _cfg(min_cash_reserve_fraction=0.0, max_total_exposure_fraction=0.0,
             max_cluster_fraction=0.25),
        equity=100.0, cash=60.0, positions=positions, peak_equity=100.0)
    assert verdict.blocked
    assert "correlated exposure" in verdict.block_reason
    assert verdict.largest_cluster == "market:same"
    assert verdict.largest_cluster_value == pytest.approx(32.0)


def test_four_unrelated_positions_are_four_bets():
    positions = [_pos(f"t{i}", 8.0, market=f"m{i}") for i in range(4)]
    verdict = riskpolicy.evaluate(
        _cfg(min_cash_reserve_fraction=0.0, max_total_exposure_fraction=0.0,
             max_cluster_fraction=0.25),
        equity=100.0, cash=60.0, positions=positions, peak_equity=100.0)
    assert not verdict.blocked


def test_a_shared_wallet_thesis_is_also_one_bet():
    positions = [_pos(f"t{i}", 14.0, wallet="whale-7") for i in range(2)]
    verdict = riskpolicy.evaluate(
        _cfg(min_cash_reserve_fraction=0.0, max_total_exposure_fraction=0.0,
             max_cluster_fraction=0.25),
        equity=100.0, cash=60.0, positions=positions, peak_equity=100.0)
    assert verdict.blocked
    assert verdict.largest_cluster == "wallet:whale-7"


def test_the_drawdown_halt_stops_entries_only():
    verdict = riskpolicy.evaluate(
        _cfg(min_cash_reserve_fraction=0.0, max_total_exposure_fraction=0.0,
             max_cluster_fraction=0.0, halt_entries_drawdown_pct=0.45),
        equity=41.86, cash=41.86, positions=[], peak_equity=100.0)
    assert verdict.blocked
    assert "only NEW entries stop" in verdict.block_reason
    assert "exits still run" in verdict.block_reason


# -- 3. the four failure modes that must be structurally impossible ---------


def test_the_scale_is_monotonically_non_increasing_in_drawdown():
    """No martingale: a bigger loss can never produce a bigger stake."""
    cfg = _cfg(shrink_from_drawdown_pct=0.15, shrink_to_drawdown_pct=0.40,
               min_size_scale=0.40)
    previous = 1.01
    for step in range(0, 101):
        scale = riskpolicy.size_scale(cfg, step / 100.0)
        assert scale <= previous + 1e-9, step
        previous = scale


def test_the_scale_never_exceeds_one():
    """No leverage escalation: this multiplier can only shrink."""
    cfg = _cfg(shrink_from_drawdown_pct=0.15, shrink_to_drawdown_pct=0.40,
               min_size_scale=0.40)
    for drawdown in (-1.0, 0.0, 0.05, 0.5, 5.0):
        assert riskpolicy.size_scale(cfg, drawdown) <= 1.0


def test_the_scale_never_falls_below_its_floor():
    """The validated strategy must keep being able to express its edge."""
    cfg = _cfg(shrink_from_drawdown_pct=0.15, shrink_to_drawdown_pct=0.40,
               min_size_scale=0.40)
    assert riskpolicy.size_scale(cfg, 0.99) == pytest.approx(0.40)


def test_there_is_no_recent_loss_term_at_all():
    """No revenge trading: the policy has no input that could express it."""
    import inspect

    source = inspect.getsource(riskpolicy)
    for forbidden in ("losing_streak", "consecutive", "recent_loss",
                      "last_trade", "recover"):
        assert forbidden not in source
    # `evaluate` takes state, never history.
    params = set(inspect.signature(riskpolicy.evaluate).parameters)
    assert params == {"cfg", "equity", "cash", "positions", "peak_equity"}


def test_the_ramp_is_linear_and_statable_in_one_sentence():
    cfg = _cfg(shrink_from_drawdown_pct=0.20, shrink_to_drawdown_pct=0.40,
               min_size_scale=0.50)
    assert riskpolicy.size_scale(cfg, 0.20) == pytest.approx(1.0)
    assert riskpolicy.size_scale(cfg, 0.30) == pytest.approx(0.75)
    assert riskpolicy.size_scale(cfg, 0.40) == pytest.approx(0.50)


def test_a_healthy_account_is_untouched():
    verdict = riskpolicy.evaluate(_cfg(), equity=100.0, cash=60.0,
                                  positions=[_pos("a", 10.0, market="m1")],
                                  peak_equity=100.0)
    assert not verdict.blocked
    assert verdict.scale == 1.0
    assert verdict.reasons == []


# -- 4. the wiring -----------------------------------------------------------


def test_the_engine_can_only_shrink_with_the_scale():
    """`_size_throttle` multiplies it in, clamped — a preservation bug can
    make a position smaller and can never make one bigger."""
    from types import SimpleNamespace

    from pqb.bridge.baseline_engine import BaselineDecisionEngine

    engine = BaselineDecisionEngine.__new__(BaselineDecisionEngine)
    calm = SimpleNamespace(regime={"regime_aggressiveness": 1.0},
                           capital_scale=1.0)
    shrunk = SimpleNamespace(regime={"regime_aggressiveness": 1.0},
                             capital_scale=0.4)
    absurd = SimpleNamespace(regime={"regime_aggressiveness": 1.0},
                             capital_scale=99.0)
    assert engine._size_throttle(calm) == pytest.approx(1.0)
    assert engine._size_throttle(shrunk) == pytest.approx(0.4)
    assert engine._size_throttle(absurd) == pytest.approx(1.0)


def test_a_context_without_the_field_behaves_exactly_as_before():
    from types import SimpleNamespace

    from pqb.bridge.baseline_engine import BaselineDecisionEngine

    engine = BaselineDecisionEngine.__new__(BaselineDecisionEngine)
    legacy = SimpleNamespace(regime={"regime_aggressiveness": 0.8})
    assert engine._size_throttle(legacy) == pytest.approx(0.8)


def test_positions_from_adapts_the_runners_view():
    class _View:
        token_id, market_id, size, avg_price = "t1", "m1", 10.0, 0.5
        wallet_influence = ""

    views = riskpolicy.positions_from([_View()], {},
                                      mark_for=lambda _t: 0.60)
    assert views[0].value == pytest.approx(6.0)
    assert views[0].market_id == "m1"


def test_the_policy_cannot_reach_an_exit_path():
    """A risk control that trapped capital in a loser would defeat itself."""
    import inspect

    source = inspect.getsource(riskpolicy)
    for forbidden in ("Action.EXIT", "close_lifecycle", "size_sell",
                      "flatten"):
        assert forbidden not in source
