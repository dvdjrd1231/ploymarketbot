"""The EV decision layer: probability, expected value, and replacement.

Guards the redesign's central claim - that a wallet score is NOT a probability,
and that nothing trades without a measured positive edge after costs.
"""

from __future__ import annotations

import time

import pytest

from pqb.decision.expected_value import EVConfig, Opportunity, evaluate, position_ev
from pqb.decision.portfolio import HeldPosition, build_plan
from pqb.decision.probability import MAX_SHIFT, MIN_WALLET_SAMPLE, ProbabilityEstimate, estimate
from pqb.models import (
    AccountState, BridgeContext, MarketFeatures, MarketStatus, OutcomeQuote,
    PositionView, WalletIntel,
)


def quote(ask=0.50, mid=None, **kw):
    base = dict(token_id="T1", outcome="Yes", bid=(ask - 0.02), ask=ask,
                mid=mid if mid is not None else ask - 0.01, spread=0.02,
                source="stream", updated_ts=time.time())
    base.update(kw)
    return OutcomeQuote(**base)


def market(**kw):
    base = dict(market_id="M1", question="Will it?", status=MarketStatus.ACTIVE,
                end_ts=int(time.time()) + 86400, liquidity=50_000.0)
    base.update(kw)
    m = MarketFeatures(**base)
    m.quotes = {"T1": quote()}
    return m


def ctx(**kw):
    base = dict(cycle_id="c", ts=time.time(), account=AccountState(balance=100.0))
    base.update(kw)
    return BridgeContext(**base)


# --- probability: the market price is the starting point --------------------

def test_with_no_evidence_we_agree_with_the_market():
    """The crowd's price is the prior. Disagreeing for free is exactly what the
    old engine did when a 0.93 wallet score bought at an ask of 0.80."""
    est = estimate(quote(ask=0.60, mid=0.59), market(), ctx(), [])
    assert est.probability == pytest.approx(0.59, abs=1e-6)
    assert est.edge == pytest.approx(0.0, abs=1e-6)
    assert est.confidence == 0.0


def test_a_thin_record_moves_nothing():
    """100% over 11 trades is not informative. Below the sample floor it
    contributes exactly zero rather than a confident-looking guess."""
    thin = WalletIntel(wallet="0xa", score=0.95, sample=MIN_WALLET_SAMPLE - 1)
    est = estimate(quote(ask=0.50), market(), ctx(), [(thin, 500.0)])
    assert est.probability == pytest.approx(0.49, abs=1e-6)
    assert est.evidence == []


def test_a_deep_record_moves_the_price():
    deep = WalletIntel(wallet="0xa", score=0.85, sample=250)
    est = estimate(quote(ask=0.50), market(), ctx(), [(deep, 2_000.0)])
    assert est.probability > 0.49
    assert est.evidence and est.evidence[0].name.startswith("wallet:")


def test_more_evidence_moves_it_further():
    modest = WalletIntel(wallet="0xa", score=0.85, sample=25)
    heavy = WalletIntel(wallet="0xb", score=0.85, sample=400)
    a = estimate(quote(ask=0.50), market(), ctx(), [(modest, 1_000.0)])
    b = estimate(quote(ask=0.50), market(), ctx(), [(heavy, 1_000.0)])
    assert b.probability > a.probability


def test_a_hedgers_evidence_is_suppressed():
    """Its buy is one leg of a riskless pair, not a view on who wins."""
    directional = WalletIntel(wallet="0xa", score=0.85, sample=250, hedge_rate=0.0)
    hedger = WalletIntel(wallet="0xb", score=0.85, sample=250, hedge_rate=0.95)
    a = estimate(quote(ask=0.50), market(), ctx(), [(directional, 2_000.0)])
    b = estimate(quote(ask=0.50), market(), ctx(), [(hedger, 2_000.0)])
    assert a.probability > b.probability
    assert b.probability == pytest.approx(0.49, abs=0.01)


def test_selling_pushes_the_probability_down():
    deep = WalletIntel(wallet="0xa", score=0.85, sample=250)
    buy = estimate(quote(ask=0.50), market(), ctx(), [(deep, 2_000.0)])
    sell = estimate(quote(ask=0.50), market(), ctx(), [(deep, -2_000.0)])
    assert sell.probability < 0.49 < buy.probability


def test_evidence_cannot_run_away_with_the_price():
    """A ceiling on how far evidence may override the entire market."""
    wallets = [(WalletIntel(wallet=f"0x{i}", score=1.0, sample=500), 50_000.0)
               for i in range(20)]
    est = estimate(quote(ask=0.50), market(), ctx(), wallets)
    assert est.probability < 0.80
    assert MAX_SHIFT == 1.0


# --- expected value ---------------------------------------------------------

def test_no_evidence_means_no_trade():
    """Agreeing with the market is the common case, and must be refused.

    With nothing to go on the honest reason is the absence of evidence, which
    is checked before the size of the edge.
    """
    est = estimate(quote(ask=0.60, mid=0.60), market(), ctx(), [])
    opp = evaluate(quote(ask=0.60), market(), est, EVConfig(), stake=10.0)
    assert not opp.acceptable
    assert "not enough evidence" in opp.reject
    assert opp.ev_per_dollar < 0          # and there was no edge either


def test_a_small_edge_is_refused_even_with_evidence():
    """Past the evidence gate, the edge itself still has to clear the bar."""
    deep = WalletIntel(wallet="0xa", score=0.56, sample=400)
    est = estimate(quote(ask=0.60, mid=0.60), market(), ctx(), [(deep, 200.0)])
    opp = evaluate(quote(ask=0.60), market(), est,
                   EVConfig(min_confidence=0.0, min_ev=0.02), stake=25.0)
    assert not opp.acceptable
    assert "edge too small" in opp.reject


def test_a_score_of_093_does_not_justify_buying_at_080():
    """The precise inference this redesign exists to stop."""
    strong = WalletIntel(wallet="0xa", score=0.93, sample=300)
    est = estimate(quote(ask=0.80, mid=0.79), market(), ctx(), [(strong, 500.0)])
    # Nudged above the market, but nowhere near 0.93: the score measures the
    # WALLET, not the likelihood of this outcome.
    assert est.probability < 0.93
    assert abs(est.probability - 0.79) < 0.15


def test_fees_matter_far_more_on_a_tiny_stake():
    est = estimate(quote(ask=0.50, mid=0.45), market(), ctx(), [])
    cfg = EVConfig(fee_per_trade_usdc=0.01, min_confidence=0.0)
    tiny = evaluate(quote(ask=0.50), market(), est, cfg, stake=0.19)
    big = evaluate(quote(ask=0.50), market(), est, cfg, stake=25.0)
    assert big.ev_per_dollar > tiny.ev_per_dollar
    # At 19c, a 1c fee each way is over 10% round trip.
    assert (big.ev_per_dollar - tiny.ev_per_dollar) > 0.09


def test_insufficient_evidence_is_refused_even_with_a_big_edge():
    est = estimate(quote(ask=0.50, mid=0.30), market(), ctx(), [])
    opp = evaluate(quote(ask=0.50), market(), est, EVConfig(min_confidence=0.5),
                   stake=25.0)
    assert not opp.acceptable
    assert "not enough evidence" in opp.reject


# --- portfolio: a full book is not a reason to stop thinking ----------------

def _held(token, ev, price=0.5):
    view = PositionView(token_id=token, market_id=f"M-{token}", outcome=token,
                        size=10.0, avg_price=price, cur_price=price)
    return HeldPosition(position=view, ev_per_dollar=ev, probability=0.5,
                        exit_price=price)


def _cand(token, ev, market_id=None):
    return Opportunity(
        token_id=token, market_id=market_id or f"M-{token}", outcome=token,
        question="q",
        estimate=ProbabilityEstimate(market_price=0.5, probability=0.6),
        entry_price=0.5, ev_per_dollar=ev, stake=10.0)


def test_a_much_better_candidate_replaces_the_weakest_holding():
    """The old engine said "the 8-position limit is full" and did nothing."""
    held = [_held("A", 0.01), _held("B", 0.03)]
    plan = build_plan([_cand("NEW", 0.20)], held, EVConfig(), max_positions=2,
                      replace_margin=0.05)
    assert plan.replace, "should displace the weakest position"
    out, incoming = plan.replace[0]
    assert out.position.token_id == "A"
    assert incoming.token_id == "NEW"


def test_a_marginally_better_candidate_does_not_churn():
    """A swap pays spread and fees both ways; near-identical edges lose money."""
    held = [_held("A", 0.10), _held("B", 0.12)]
    plan = build_plan([_cand("NEW", 0.12)], held, EVConfig(), max_positions=2,
                      replace_margin=0.05)
    assert not plan.replace
    assert any("swap margin" in n for n in plan.notes)


def test_free_slots_are_filled_before_anything_is_churned():
    held = [_held("A", 0.01)]
    plan = build_plan([_cand("NEW", 0.20)], held, EVConfig(), max_positions=4)
    assert plan.enter and not plan.replace


def test_a_position_whose_edge_is_gone_is_exited():
    held = [_held("A", -0.10)]
    plan = build_plan([], held, EVConfig(exit_ev=-0.03), max_positions=4)
    assert plan.exit and plan.exit[0][0].position.token_id == "A"


def test_one_outcome_per_market():
    plan = build_plan([_cand("T1", 0.30, market_id="M1"),
                       _cand("T2", 0.25, market_id="M1")],
                      [], EVConfig(), max_positions=4)
    assert len(plan.enter) == 1


# --- holding is re-decided every cycle --------------------------------------

def test_holding_is_priced_like_a_fresh_buy():
    good = ProbabilityEstimate(market_price=0.5, probability=0.70)
    bad = ProbabilityEstimate(market_price=0.5, probability=0.30)
    assert position_ev(0.50, good, EVConfig()) > 0
    assert position_ev(0.50, bad, EVConfig()) < 0


# --- risk controls are NOT negotiable by the model --------------------------

def test_a_losing_position_is_still_stopped_out_under_the_ev_engine():
    """The EV model must never be able to talk the engine out of a stop loss.

    Reported symptom: "it refuses to get out of losing positions". The exit
    ladder runs BEFORE any expected-value reasoning, so a stop fires whatever
    the model believes the position is worth.
    """
    import inspect
    from pqb.bridge.ev_engine import EVDecisionEngine
    src = inspect.getsource(EVDecisionEngine.evaluate)
    # The ladder is consulted first, and its verdict short-circuits.
    assert "_evaluate_position" in src
    ladder_first = src.index("_evaluate_position") < src.index("_candidates")
    assert ladder_first, "risk controls must be evaluated before opportunities"
    assert "Action.EXIT" in src and "continue" in src


def test_the_ladder_itself_still_stops_a_loser():
    from pqb.bridge.baseline_engine import BaselineDecisionEngine
    from pqb.config import EngineConfig
    engine = BaselineDecisionEngine(EngineConfig())
    losing = PositionView(token_id="T1", market_id="M1", outcome="Yes",
                          size=100.0, avg_price=0.40, cur_price=0.20,
                          peak_price=0.40)
    verdict = engine._evaluate_position(losing, ctx(markets={"M1": market()},
                                                   positions=[losing]))
    assert verdict.action.value == "EXIT"
    assert verdict.exit_style == "stop"
