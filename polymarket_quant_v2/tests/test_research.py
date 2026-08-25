"""Feature inertness, winner/loser decomposition, exits, and the AI's limits."""

from __future__ import annotations

import pytest

from conftest import make_obs
from pqv2.research import ai, exits, features, winners
from pqv2.strategy_b.strategy import EXIT_SETTLEMENT, naive_copy
from pqv2.substrate.data import PriceTape, oos_split_ts
from pqv2.substrate.state import collect
from pqv2.validation.backtest import Fill


# --- feature inertness ------------------------------------------------------

def test_a_constant_feature_is_reported_inert():
    obs = [make_obs(w_settled_n=0) for _ in range(80)]
    v = features.evaluate_feature(obs, "w_settled_n")
    assert v.inert
    assert "no-op" in v.note


def test_a_varying_feature_is_not_inert():
    obs = [make_obs(price=0.1 + i * 0.01) for i in range(60)]
    v = features.evaluate_feature(obs, "price")
    assert not v.inert
    assert v.distinct > 3


def test_inert_features_kill_the_axes_that_depend_on_them():
    """The finding that was actually costing this project multiple-testing
    power: three axes are inert because settled_ts is never populated."""
    obs = [make_obs(w_settled_n=0, w_roll_win_rate=0.0, w_consec_losses=0,
                    price=0.2 + (i % 40) * 0.015, rel_notional=1.0 + i % 3,
                    market_recent_prints=i % 12,
                    market_price_move=0.01 * (i % 5))
           for i in range(200)]
    audit = features.audit_features(obs)
    assert "min_settled_n" in audit["dead_axes"]
    assert "min_roll_win_rate" in audit["dead_axes"]
    assert "max_consec_losses" in audit["dead_axes"]
    assert audit["grid_effective"] < audit["grid_nominal"]
    assert audit["wasted_multiple_testing_factor"] > 1.0


def test_feature_audit_on_real_shape_reports_the_grid_cost(st):
    obs = collect(st, limit=2000)
    audit = features.audit_features(obs)
    assert audit["grid_nominal"] >= audit["grid_effective"] >= 1
    assert isinstance(audit["note"], str) and audit["note"]


# --- winner / loser ---------------------------------------------------------

def _fills(rets):
    return [Fill(ts=i, token_id=f"t{i}", market_id=f"m{i % 5}", wallet="w",
                 entry=0.5, exit_price=0.5 * (1 + r), stake=100.0, ret=r,
                 pnl=100.0 * r, won=r > 0, hold_secs=3600,
                 exit_reason="settlement", rel_notional=1.0)
            for i, r in enumerate(rets)]


def test_buckets_are_on_return_not_dollars():
    assert winners.bucket_of(2.0) == "MONSTER_WINNER"
    assert winners.bucket_of(0.5) == "LARGE_WINNER"
    assert winners.bucket_of(-0.05) == "SMALL_LOSER"
    assert winners.bucket_of(-0.9) == "LARGE_LOSER"


def test_high_win_rate_with_bad_asymmetry_is_called_out():
    """The shape that looks excellent until the tail arrives."""
    rets = [0.05] * 85 + [-1.0] * 15
    out = winners.decompose(_fills(rets))
    a = out["asymmetry"]
    assert a["win_rate"] > 0.8
    assert a["win_loss_ratio"] < 0.5
    assert "until the tail arrives" in out["note"]


def test_good_asymmetry_is_recognised():
    rets = [1.2] * 40 + [-0.25] * 60
    out = winners.decompose(_fills(rets))
    assert "asymmetry the brief asks for is present" in out["note"]


def test_tail_dependence_is_flagged():
    rets = [10.0] * 3 + [0.001] * 97
    out = winners.decompose(_fills(rets))
    assert out["asymmetry"]["tail_dependence_top5pct"] > 0.8
    assert "IS its tail" in out["note"]


def test_separating_features_only_uses_entry_time_fields():
    """Explaining the outcome with the outcome would make every finding
    circular."""
    for feat in winners.ENTRY_FEATURES:
        assert feat not in ("ret", "pnl", "won", "exit_price", "exit_reason")


def test_separating_features_says_so_when_the_tails_are_thin():
    out = winners.separating_features(_fills([0.5] * 10 + [-0.9] * 3))
    assert "need" in out[0].get("note", "")


# --- exits ------------------------------------------------------------------

def test_exit_grid_always_contains_settlement():
    models = [r.model for r in exits.exit_grid()]
    assert EXIT_SETTLEMENT in models


def test_settlement_results_are_exact_and_others_are_modelled(st):
    from pqv2.validation import backtest
    from pqv2.strategy_b.strategy import EXIT_TARGET, ExitRule
    obs = collect(st, wallets=["0xedge"], limit=200)
    tape = PriceTape(st)
    s = naive_copy("0xedge", delay_secs=0)
    exact = backtest.run(s, obs, st, tape)
    modelled = backtest.run(s.with_exit(ExitRule(model=EXIT_TARGET)), obs, st,
                            tape)
    assert exact.exit_confidence == "exact"
    assert modelled.exit_confidence == "modelled"


def test_a_thin_early_exit_win_does_not_unseat_settlement():
    """A modelled result must not beat an exact one on a thin margin."""
    best = {"exit": "target +15%", "model": "profit_target",
            "expectancy": 0.1050, "win_loss_ratio": 1.0, "tail_loss_p05": -0.5}
    settlement = {"exit": "settlement", "model": EXIT_SETTLEMENT,
                  "expectancy": 0.1000, "win_loss_ratio": 1.0,
                  "tail_loss_p05": -0.5}
    v = exits._verdict(best, settlement, [best, settlement])
    assert "Not sufficient to switch" in v


def test_settlement_winning_is_stated_as_preferable():
    row = {"exit": "settlement", "model": EXIT_SETTLEMENT, "expectancy": 0.2}
    v = exits._verdict(row, row, [row])
    assert "exact rather than modelled" in v


# --- the AI's limits --------------------------------------------------------

def test_hypotheses_are_always_only_proposed():
    r = ai.Researcher()
    out = r.propose(pass_report={"status_histogram": [["FAILED", 90]],
                                 "agreement": []})
    assert out
    assert all(h.status == "PROPOSED" for h in out)


def test_every_hypothesis_carries_a_test_and_a_falsifier():
    r = ai.Researcher()
    out = r.propose(
        pass_report={"status_histogram": [["NO_WALLET_ALPHA", 120]],
                     "agreement": []},
        feature_audit={"dead_axes": ["min_settled_n"], "grid_nominal": 5184,
                       "grid_effective": 432,
                       "wasted_multiple_testing_factor": 12.0,
                       "inert_features": ["w_settled_n"], "note": "x"})
    assert out
    for h in out:
        assert h.test and h.predicts and h.falsifies


def test_a_broken_backend_never_stops_research():
    class Boom:
        def propose(self, **kw):
            raise RuntimeError("no api key")

    out = ai.Researcher(backend=Boom()).propose(
        pass_report={"status_histogram": [], "agreement": []})
    assert any(h.source == "backend_error" for h in out)
    assert any(h.source == "offline_rules" for h in out) or len(out) >= 1


def test_llm_output_cannot_arrive_pre_validated():
    class Sneaky:
        def propose(self, **kw):
            h = ai.Hypothesis(hypothesis_id="X", claim="trust me",
                              rationale="", test="", predicts="",
                              falsifies="")
            h.status = "VALIDATED"          # try to promote itself
            return [h]

    out = ai.Researcher(backend=Sneaky()).propose(
        pass_report={"status_histogram": [], "agreement": []})
    assert all(h.status == "PROPOSED" for h in out)


def test_transferable_rule_is_the_top_priority_hypothesis():
    r = ai.Researcher()
    out = r.propose(pass_report={
        "status_histogram": [["VALIDATED", 4]],
        "agreement": [{"rule_id": "R1", "describe": "d", "wallets_validated": 3,
                       "wallets_positive": 5, "wallets_tested": 6,
                       "mean_alpha": 0.04, "cross_wallet_t": 3.1}]})
    assert out[0].priority == 1.0
    assert "held-out" in out[0].test.lower()
