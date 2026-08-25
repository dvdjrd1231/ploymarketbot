"""The Strategy B route end to end, and the funnel arithmetic that guards it."""

from __future__ import annotations

import pytest

from conftest import make_obs
from pqv2.config import Settings
from pqv2.ledger import Funnel, Mode, Stage
from pqv2.risk.compounding import new_account
from pqv2.risk.sizing import ExpansionEvidence
from pqv2.strategy_b.behavior import BehaviorMatcher, CompositeMatcher, WEIGHTS
from pqv2.strategy_b.decompose import build_profile
from pqv2.strategy_b.engine import StrategyBEngine, StrategyBinding
from pqv2.strategy_b.strategy import CopyStrategy, naive_copy
from pqv2.substrate.data import PriceTape, oos_split_ts
from pqv2.substrate.state import collect


# --- behaviour matching -----------------------------------------------------

def test_behaviour_weights_sum_to_one():
    """A silent drift in the sum turns the match threshold into a different
    threshold without anyone changing it."""
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_matcher_scores_an_in_profile_signal_above_an_out_of_profile_one(st):
    obs = collect(st, wallets=["0xedge"], ts_to=oos_split_ts(st))
    if len(obs) < 30:
        pytest.skip("fixture too small")
    m = BehaviorMatcher(build_profile("0xedge", obs))
    p50 = sorted(o.price for o in obs)[len(obs) // 2]
    in_profile = m.score(make_obs(price=p50, rel_notional=1.0))
    out_of_profile = m.score(make_obs(price=0.995, rel_notional=40.0,
                                      market_recent_prints=0))
    assert in_profile.score > out_of_profile.score


def test_unknown_horizon_scores_no_opinion_not_free_agreement(st):
    """Handing out 1.0 where we know least is how a matcher fools itself."""
    obs = collect(st, wallets=["0xedge"], limit=100)
    m = BehaviorMatcher(build_profile("0xedge", obs))
    r = m.score(make_obs(settled_ts=0))
    assert r.components["horizon"] == 0.5


def test_family_matcher_uses_the_median_not_the_best(st):
    """Taking the best of N would make a family score higher the more members
    it has, rewarding size instead of agreement."""
    obs = collect(st, limit=600)
    wallets = sorted({o.trade.wallet for o in obs})[:3]
    profiles = [build_profile(w, obs) for w in wallets]
    profiles = [p for p in profiles if p.n_observations >= 10]
    if len(profiles) < 3:
        pytest.skip("fixture too small")
    comp = CompositeMatcher(profiles)
    o = make_obs(price=0.6)
    singles = sorted(BehaviorMatcher(p).score(o).score for p in profiles)
    assert comp.score(o).score == pytest.approx(singles[len(singles) // 2],
                                                abs=1e-6)


# --- the route ---------------------------------------------------------------

def _engine(st, wallet, profile, *, mode=Mode.SHADOW.value, status="VALIDATED",
            strategy=None):
    account = new_account(st)
    binding = StrategyBinding(
        strategy=strategy or naive_copy(wallet, delay_secs=0),
        matcher=BehaviorMatcher(profile), status=status,
        evidence=ExpansionEvidence(size_predicts_win=0.1), mode=mode)
    return StrategyBEngine(st, account, bindings=[binding],
                           tape=PriceTape(st), mode=mode, funnel=Funnel())


def test_every_signal_reaches_exactly_one_terminal_state(st):
    obs = collect(st, wallets=["0xedge"], ts_from=oos_split_ts(st))
    if len(obs) < 20:
        pytest.skip("fixture too small")
    eng = _engine(st, "0xedge", build_profile("0xedge", obs))
    for o in obs:
        eng.evaluate(o)
    eng.funnel.assert_balanced()
    r = eng.funnel.reconcile("B")
    terminal = (r["strategy_rejected"] + r["risk_rejected"]
                + r["portfolio_rejected"] + r["execution_failed"]
                + r["execution_successful"])
    assert terminal + r["in_flight"] == r["received"]


def test_every_rejection_carries_a_registered_gate_and_a_reason(st):
    from pqv2.gates import REGISTRY
    obs = collect(st, wallets=["0xedge"], ts_from=oos_split_ts(st))
    if len(obs) < 20:
        pytest.skip("fixture too small")
    eng = _engine(st, "0xedge", build_profile("0xedge", obs))
    for o in obs:
        eng.evaluate(o)
    for rec in eng.records:
        if Stage(rec.stage).terminal and rec.stage != "EXECUTION_SUCCESSFUL":
            assert rec.gate_key in REGISTRY, rec.stage
            assert rec.reason, "a rejection with no reason breaks rule 6"


def test_no_strategy_a_gate_ever_appears_on_route_b(st):
    from pqv2.gates import REGISTRY, Owner
    obs = collect(st, wallets=["0xedge"], ts_from=oos_split_ts(st))
    if len(obs) < 20:
        pytest.skip("fixture too small")
    eng = _engine(st, "0xedge", build_profile("0xedge", obs))
    for o in obs:
        eng.evaluate(o)
    for rec in eng.records:
        if rec.gate_key:
            assert REGISTRY[rec.gate_key].owner is not Owner.STRATEGY_A


def test_strategy_a_is_recorded_as_not_consulted(st):
    obs = collect(st, wallets=["0xedge"], limit=30)
    eng = _engine(st, "0xedge", build_profile("0xedge", obs))
    recs = [r for o in obs for r in eng.evaluate(o)]
    assert recs
    assert all(r.strategy_a_result == "NOT_CONSULTED" for r in recs)


def test_unvalidated_strategy_cannot_reach_paper_but_can_reach_shadow(st):
    obs = collect(st, wallets=["0xedge"], limit=80)
    profile = build_profile("0xedge", obs)

    paper = _engine(st, "0xedge", profile, mode=Mode.PAPER.value,
                    status="FAILED")
    recs = [r for o in obs for r in paper.evaluate(o)]
    assert recs
    assert all(r.gate_key == "b.strategy_not_validated"
               for r in recs if r.gate_key)

    shadow = _engine(st, "0xedge", profile, mode=Mode.SHADOW.value,
                     status="FAILED")
    recs = [r for o in obs for r in shadow.evaluate(o)]
    assert any(r.stage != "STRATEGY_REJECTED" for r in recs), (
        "SHADOW must run on any status; that is how evidence accumulates "
        "without the V1 deadlock")


def test_live_mode_refuses_to_promote_itself(st):
    obs = collect(st, wallets=["0xedge"], limit=40)
    eng = _engine(st, "0xedge", build_profile("0xedge", obs),
                  mode=Mode.LIVE.value, status="VALIDATED")
    recs = [r for o in obs for r in eng.evaluate(o)]
    assert recs
    assert all("human promotion" in r.reason for r in recs if r.reason)


def test_depth_and_spread_are_none_not_zero_on_this_substrate(st):
    """A zero would read as 'measured and empty' and silently justify a depth
    rejection. There is no historical order book; None says so."""
    obs = collect(st, wallets=["0xedge"], limit=25)
    eng = _engine(st, "0xedge", build_profile("0xedge", obs))
    recs = [r for o in obs for r in eng.evaluate(o)]
    assert recs
    assert all(r.depth is None and r.spread is None for r in recs)


def test_account_never_goes_below_zero_through_the_route(st):
    obs = collect(st, wallets=["0xedge"], ts_from=oos_split_ts(st))
    if len(obs) < 20:
        pytest.skip("fixture too small")
    eng = _engine(st, "0xedge", build_profile("0xedge", obs))
    for o in obs:
        eng.evaluate(o)
    assert eng.account.allocated <= eng.account.equity
    eng.account.check()


def test_engine_report_refuses_to_render_an_unbalanced_funnel(st):
    obs = collect(st, wallets=["0xedge"], limit=30)
    eng = _engine(st, "0xedge", build_profile("0xedge", obs))
    for o in obs:
        eng.evaluate(o)
    eng.funnel.stages["B"][Stage.EXECUTION_SUCCESSFUL.value] += 999
    with pytest.raises(AssertionError, match="unexplained gap"):
        eng.report()
