"""Gates, agents and the debate.

The properties under test are the ones that stop V1's pathology recurring:
every gate runs even after one fails, abstention is not agreement, and the red
team is a veto rather than a vote.
"""

from __future__ import annotations

import pytest

from pqv3.agents.base import Stance, Verdict, agree
from pqv3.agents.debate import Debate
from pqv3.agents.registry import AGENTS, ADVERSARIAL
from pqv3.core.canon import Availability, EvidenceState, Layer
from pqv3.decision.gates import GATES, GateRunner, Owner
from pqv3.portfolio.capital import Account, Feasibility, SizingResult


def _full_state(as_of: int = 1000) -> EvidenceState:
    ev = EvidenceState(as_of=as_of, market_id="m", token_id="t")
    for layer in ev.layers():
        layer.availability = Availability.OK
        layer.as_of = as_of - 10
        layer.age_secs = 10
        layer.rows = 5
    ev.price.data = {"last": 0.50, "velocity_1h": 0.01, "acceleration": 0.0,
                     "volatility_1h": 0.01, "prints_1h": 20, "gap": 0.0}
    ev.liquidity.data = {"prints_per_hour": 20, "notional_per_hour": 500.0,
                         "liquidity_ratio": 1.0}
    ev.market.data = {"question": "q", "close_ts": as_of + 86_400,
                      "status": "OPEN", "event_id": "e"}
    ev.risk.data = {"open_positions": 0, "gross_exposure": 0.0,
                    "by_correlation": {}}
    ev.execution.data = {"reference_price": 0.5, "spread": 0.01,
                         "spread_measured": True, "uncertainty": []}
    ev.regime.data = {"primary": "STABLE", "flags": ["STABLE"],
                      "confidence": 1.0, "measurements": {}}
    return ev


def _ok_sizing() -> SizingResult:
    return SizingResult(feasibility=Feasibility.OK, size_usdc=4.0,
                        size_shares=8.0, entry_price=0.5025, signal_price=0.5,
                        max_loss=4.0, expected_value=0.30,
                        fill_probability=1.0, available_liquidity=500.0)


# --------------------------------------------------------------------- gates
def test_there_are_twelve_gates():
    assert len(GATES) == 12
    assert len({g.name for g in GATES}) == 12


def test_every_gate_has_an_owner_and_a_rationale():
    for g in GATES:
        assert isinstance(g.owner, Owner)
        assert len(g.rationale) > 20, f"{g.name} has no written rationale"


def test_all_gates_run_even_after_one_fails(st):
    """V1's pathology: one gate above all others hid eleven more.

    40,820 of 40,820 decisions were blocked by a single gate, so nobody could
    see that the gates below it had never been reached. Short-circuiting is how
    that becomes invisible.
    """
    ev = _full_state()
    ev.price.availability = Availability.STALE      # break the first gate
    rep = GateRunner(st).run(
        ev=ev, account=Account(), sizing=_ok_sizing(), signal_strength=1.0,
        fair_probability=0.6, market_probability=0.5, confidence=0.8)
    assert len(rep.results) == 12, "gates short-circuited on first failure"
    assert not rep.passed
    assert rep.blocking_gate == "DATA_VALIDITY"
    assert len(rep.blocking) > 1, "later gates were not evaluated"


def test_missing_evidence_fails_rather_than_passes(st):
    """A gate that cannot judge must refuse, not wave it through."""
    ev = EvidenceState(as_of=1000)                  # every layer UNAVAILABLE
    rep = GateRunner(st).run(
        ev=ev, account=Account(), sizing=_ok_sizing(), signal_strength=1.0,
        fair_probability=0.6, market_probability=0.5, confidence=0.9)
    assert not rep.passed
    names = {r.gate for r in rep.blocking}
    assert "DATA_VALIDITY" in names
    assert "INFORMATION_VALIDITY" in names


def test_leak_detection_fires(st):
    ev = _full_state(as_of=1000)
    ev.news.as_of = 5000                            # dated after as_of
    rep = GateRunner(st).run(
        ev=ev, account=Account(), sizing=_ok_sizing(), signal_strength=1.0,
        fair_probability=0.6, market_probability=0.5, confidence=0.9)
    info = next(r for r in rep.results if r.gate == "INFORMATION_VALIDITY")
    assert not info.passed
    assert "LEAK" in info.reason


def test_perfect_win_rate_demands_more_evidence_not_less(st):
    """'If the model discovers an apparent perfect strategy: INCREASE VALIDATION.'"""
    ev = _full_state()
    strategy = {"strategy_id": "s", "status": "PAPER", "trade_count": 40,
                "win_rate": 1.0, "expectancy": 0.2, "oos_expectancy": 0.2,
                "walkforward_positive": 1.0, "p_value": 0.001,
                "bh_threshold": 0.05, "hypotheses_tested": 100}
    rep = GateRunner(st).run(
        ev=ev, account=Account(), sizing=_ok_sizing(), signal_strength=1.0,
        fair_probability=0.6, market_probability=0.5, confidence=0.9,
        strategy=strategy, red_team={"killed": False, "consensus": 0.9,
                                     "model_disagreement": 0.1, "n_agents": 10})
    oos = next(r for r in rep.results if r.gate == "OUT_OF_SAMPLE_VALIDITY")
    assert not oos.passed
    assert "perfect" in oos.reason.lower() or "sampling" in oos.reason.lower()


def test_p_value_without_a_denominator_is_rejected(st):
    ev = _full_state()
    strategy = {"strategy_id": "s", "trade_count": 100, "p_value": 0.0001,
                "bh_threshold": 0.05, "hypotheses_tested": 0}
    rep = GateRunner(st).run(
        ev=ev, account=Account(), sizing=_ok_sizing(), signal_strength=1.0,
        fair_probability=0.6, market_probability=0.5, confidence=0.9,
        strategy=strategy)
    stat = next(r for r in rep.results if r.gate == "STATISTICAL_VALIDITY")
    assert not stat.passed
    assert "denominator" in stat.reason


def test_unreviewed_thesis_fails_adversarial_gate(st):
    ev = _full_state()
    rep = GateRunner(st).run(
        ev=ev, account=Account(), sizing=_ok_sizing(), signal_strength=1.0,
        fair_probability=0.6, market_probability=0.5, confidence=0.9,
        red_team={})
    adv = next(r for r in rep.results if r.gate == "ADVERSARIAL_VALIDITY")
    assert not adv.passed


# -------------------------------------------------------------------- agents
def test_there_are_twenty_five_agents():
    assert len(AGENTS) == 25
    assert len({a.name for a in AGENTS}) == 25
    assert {a.number for a in AGENTS} == set(range(1, 26))


def test_adversarial_subset_is_non_trivial():
    assert len(ADVERSARIAL) >= 5
    assert any(a.name == "RED_TEAM" for a in ADVERSARIAL)


def test_agent_abstains_when_its_layer_is_missing():
    ev = EvidenceState(as_of=1000)
    news = next(a for a in AGENTS if a.name == "NEWS_INTELLIGENCE")
    v = news.run(ev, {})
    assert v.stance is Stance.ABSTAIN
    assert "news" in v.abstain_reason


def test_abstention_carries_zero_weight_not_neutral_agreement():
    """With most layers empty, counting abstentions as neutral would let a
    four-agent quorum masquerade as a twenty-five-agent consensus."""
    vs = [Verdict("a", Stance.FOR, 0.9), Verdict("b", Stance.ABSTAIN, 0.0),
          Verdict("c", Stance.ABSTAIN, 0.0)]
    out = agree(vs)
    assert out["consensus"] == 1.0
    assert out["n_active"] == 1
    assert out["n_abstained"] == 2
    assert Verdict("b", Stance.ABSTAIN, 0.9).weight == 0.0


def test_a_raising_agent_does_not_become_a_pass():
    def boom(ev, ctx):
        raise RuntimeError("kaboom")

    from pqv3.agents.base import AgentSpec
    spec = AgentSpec(99, "BOOM", "explodes", (), boom)
    v = spec.run(EvidenceState(as_of=1), {})
    assert v.stance is Stance.ABSTAIN
    assert "kaboom" in v.abstain_reason


def test_red_team_is_a_veto_not_a_weight(st):
    """A sufficiently enthusiastic majority must not be able to outvote it."""
    ev = EvidenceState(as_of=1000)                 # thin evidence on purpose
    res = Debate(st).run(ev, {"market_probability": 0.5})
    assert res.killed or res.confidence == 0.0, (
        "a state with almost no evidence produced a live, confident verdict")
    if res.killed:
        assert res.confidence == 0.0
        assert any("VETO" in o for o in res.objections)


def test_debate_records_who_abstained_and_why(st):
    ev = EvidenceState(as_of=1000)
    res = Debate(st).run(ev, {"market_probability": 0.5})
    assert res.abstentions, "abstentions were hidden"
    assert all(a["reason"] for a in res.abstentions)
    assert res.stats["n_agents"] == 25


def test_confidence_falls_with_incomplete_information(st):
    thin = Debate(st).run(EvidenceState(as_of=1000), {"market_probability": 0.5})
    assert thin.confidence <= 0.5
