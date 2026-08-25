"""The V2 two-route architecture, and the rule it exists to enforce.

The non-negotiable rule from the master prompt is that Strategy B must not be
silently blocked by Strategy A. These tests pin it as a property of the router
rather than as a convention, because on the measured build the same class of
failure cost the account 40,820 consecutive DO_NOTHING decisions and every
single one of them looked reasonable in isolation.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pqv2 import gatemap                                       # noqa: E402
from pqv2.funnel import (EXECUTION_SUCCESSFUL, Opportunity,    # noqa: E402
                         OpportunityLedger, PORTFOLIO_APPROVED,
                         PORTFOLIO_REJECTED, RISK_REJECTED, ROUTE_A, ROUTE_B,
                         STRATEGY_ACCEPTED, STRATEGY_REJECTED, counters,
                         funnel, suppression_ranking)
from pqv2.router import (CopySpec, GateVerdict, Router,        # noqa: E402
                         load_validated, signal_from)


@pytest.fixture
def ledger(tmp_path):
    led = OpportunityLedger(tmp_path / "opp.sqlite3")
    yield led
    led.close()


def opp(route=ROUTE_B, **kw):
    base = dict(route=route, market="m1", token="t1", wallet="0xabc",
                strategy_id="S1", behavior_match=0.8, entry_price=0.80)
    base.update(kw)
    return Opportunity(**base)


# ===========================================================================
# THE RULE
# ===========================================================================

def test_a_strategy_a_gate_cannot_block_a_route_b_signal(ledger):
    """The whole patch, as one assertion.

    `learning_mode` is what stopped 100% of the measured build's trades. It is
    Strategy A's opinion about Strategy A's edge, so presented against a
    route-B signal it must not terminate it.
    """
    router = Router(ledger, mode="shadow")
    signal = opp(route=ROUTE_B)
    result = router.submit(signal, strategy_gates=[
        GateVerdict("learning_mode", passed=False,
                    reason="Learning mode: no validated strategies yet")])

    assert result.state != STRATEGY_REJECTED
    assert result.state == PORTFOLIO_APPROVED
    # ...and the near-miss is recorded rather than silently discarded.
    assert router.inherited_gates
    assert router.inherited_gates[0]["gate"] == "learning_mode"
    assert "NOT APPLIED" in router.inherited_gates[0]["action"]
    assert "would have rejected" in result.strategy_a_result


def test_the_same_gate_does_block_route_a(ledger):
    """Strategy A keeps its own filters. The patch does not weaken A."""
    router = Router(ledger, mode="shadow")
    result = router.submit(opp(route=ROUTE_A), strategy_gates=[
        GateVerdict("learning_mode", passed=False, reason="learning mode")])
    assert result.state == STRATEGY_REJECTED
    assert result.rejected_by == "learning_mode"


@pytest.mark.parametrize("gate", [g.key for g in
                                  gatemap.gates_for(gatemap.STRATEGY_A)])
def test_no_strategy_a_gate_can_touch_route_b(ledger, gate):
    """Every one of them, not just the one that happened to bite."""
    router = Router(ledger, mode="shadow")
    result = router.submit(opp(route=ROUTE_B), strategy_gates=[
        GateVerdict(gate, passed=False, reason="A says no")])
    assert result.state == PORTFOLIO_APPROVED


@pytest.mark.parametrize("gate", [g.key for g in
                                  gatemap.gates_for(gatemap.GLOBAL_SAFETY)])
def test_every_global_safety_gate_still_blocks_route_b(ledger, gate):
    """Route B is independent of Strategy A. It is not independent of the
    account — no quote, no cash, halted, or already resolving still stop it."""
    router = Router(ledger, mode="shadow")
    result = router.submit(opp(route=ROUTE_B), strategy_gates=[
        GateVerdict(gate, passed=False, reason="global safety")])
    assert result.state == STRATEGY_REJECTED
    assert result.rejected_owner == gatemap.GLOBAL_SAFETY


def test_portfolio_and_execution_gates_bind_both_routes(ledger):
    router = Router(ledger, mode="shadow")
    for route in (ROUTE_A, ROUTE_B):
        result = router.submit(opp(route=route), portfolio_gates=[
            GateVerdict("cluster_cap", passed=False,
                        reason="correlated exposure at the cap")])
        assert result.state == PORTFOLIO_REJECTED
        assert result.rejected_owner == gatemap.PORTFOLIO


def test_an_unclassified_gate_blocks_rather_than_waves_through(ledger):
    """A rejection nobody has classified is a rejection nobody understands.

    Defaulting to 'allow' would let an unmapped veto quietly become
    load-bearing; defaulting to 'block' makes it show up in the audit as
    unclassified until somebody adds it to the map.
    """
    router = Router(ledger, mode="shadow")
    result = router.submit(opp(route=ROUTE_B), strategy_gates=[
        GateVerdict("something_nobody_mapped", passed=False, reason="?")])
    assert result.state == STRATEGY_REJECTED
    assert result.rejected_owner == "UNCLASSIFIED"


# ===========================================================================
# no silent blocks
# ===========================================================================

def test_a_rejection_always_names_a_gate_and_a_reason(ledger):
    router = Router(ledger, mode="shadow")
    result = router.submit(opp(), risk_gates=[
        GateVerdict("drawdown_halt", passed=False,
                    reason="account is 46% below its peak")])
    assert result.state == RISK_REJECTED
    assert result.rejected_by == "drawdown_halt"
    assert result.reason
    row = ledger.rows()[0]
    assert row["rejected_by"] == "drawdown_halt"
    assert row["reason"]
    assert json.loads(row["trail"])          # the full path is persisted


def test_every_submitted_signal_lands_in_the_ledger(ledger):
    router = Router(ledger, mode="shadow")
    router.submit(opp())
    router.submit(opp(), strategy_gates=[
        GateVerdict("cash_reserve", passed=False, reason="no cash")])
    assert len(ledger.rows()) == 2


def test_the_funnel_cannot_run_backwards():
    signal = opp()
    signal.advance(STRATEGY_ACCEPTED)
    with pytest.raises(ValueError):
        signal.advance("SIGNAL_RECEIVED")


def test_suppression_ranking_flags_an_inherited_gate_as_a_wiring_bug():
    """If an A-gate ever does terminate a B signal, the audit must say so in
    those words rather than presenting it as a tuning observation."""
    rows = [{"route": ROUTE_B, "state": STRATEGY_REJECTED,
             "rejected_by": "hc_market_state"} for _ in range(12)]
    ranked = suppression_ranking(rows, ROUTE_B)
    assert ranked[0]["gate"] == "hc_market_state"
    assert ranked[0]["entitledToBlockThisRoute"] is False
    assert "INHERITED" in ranked[0]["verdict"]


def test_routes_keep_separate_counters(ledger):
    router = Router(ledger, mode="shadow")
    router.submit(opp(route=ROUTE_A))
    router.submit(opp(route=ROUTE_B))
    router.submit(opp(route=ROUTE_B), risk_gates=[
        GateVerdict("cash_reserve", passed=False, reason="no cash")])
    out = counters(ledger.rows())
    assert out[ROUTE_A]["signals"] == 1
    assert out[ROUTE_B]["signals"] == 2
    assert out[ROUTE_B]["riskRejected"] == 1
    assert out[ROUTE_A]["riskRejected"] == 0


# ===========================================================================
# shadow mode and execution
# ===========================================================================

def test_shadow_mode_never_calls_the_execution_adapter(ledger):
    calls = []
    router = Router(ledger, mode="shadow",
                    execute=lambda o: calls.append(o) or True)
    result = router.submit(opp())
    assert not calls
    assert result.state == PORTFOLIO_APPROVED
    assert "not sent" in result.execution_result


def test_live_mode_executes_and_records_the_fill(ledger):
    router = Router(ledger, mode="live", execute=lambda o: True)
    result = router.submit(opp())
    assert result.state == EXECUTION_SUCCESSFUL
    assert counters(ledger.rows())[ROUTE_B]["executionSuccessful"] == 1


def test_an_execution_error_is_recorded_not_raised(ledger):
    def boom(_):
        raise RuntimeError("venue timeout")

    router = Router(ledger, mode="live", execute=boom)
    result = router.submit(opp())
    assert result.state == "EXECUTION_FAILED"
    assert "venue timeout" in result.execution_result


# ===========================================================================
# the validated specs
# ===========================================================================

def test_only_validated_specs_are_loaded(tmp_path):
    """52 of the 54 experiments on the real registry are not validated, and
    none of them may emit a signal."""
    path = tmp_path / "experiments.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE experiments (wallet TEXT, spec_hash TEXT, "
                 "score REAL, oos_p REAL, spec_json TEXT, status TEXT)")
    spec = json.dumps({"min_price": 0.5, "max_price": 0.98, "delay_secs": 0,
                       "stake_mode": "flat", "stake_fraction": 0.05})
    for status in ("VALIDATED", "OVERFIT", "FAILED", "NOT_SIGNIFICANT",
                   "INSUFFICIENT_EVIDENCE"):
        conn.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?)",
                     (f"0x{status[:6]}", "h" * 16, 0.8, 0.001, spec, status))
    conn.commit()
    conn.close()

    specs = load_validated(path)
    assert len(specs) == 1
    assert specs[0].wallet.startswith("0xVALIDA")


def test_a_missing_registry_is_empty_not_an_error(tmp_path):
    assert load_validated(tmp_path / "nope.sqlite3") == []


def test_the_spec_price_band_is_the_specs_own_not_strategy_as():
    """The real validated spec trades 0.70-0.98. Strategy A's band stops at
    0.95, so applying A's band would delete most of B's edge — this pins that
    B is matched against its own numbers."""
    spec = CopySpec(wallet="0xabc", strategy_id="S", score=0.74,
                    min_price=0.70, max_price=0.98)
    matched, confidence, _ = spec.matches(price=0.97,
                                          seconds_since_wallet_trade=0)
    assert matched and confidence == 0.74
    matched, _, why = spec.matches(price=0.60,
                                   seconds_since_wallet_trade=0)
    assert not matched and "floor" in why


def test_the_validated_entry_delay_is_honoured():
    """One real spec enters 300s after the wallet. Copying sooner is a
    different strategy from the one that was validated."""
    spec = CopySpec(wallet="0xabc", strategy_id="S", delay_secs=300.0,
                    min_price=0.0, max_price=1.0)
    assert not spec.matches(0.8, seconds_since_wallet_trade=60)[0]
    assert spec.matches(0.8, seconds_since_wallet_trade=301)[0]


def test_the_flat_backtest_stake_is_not_copied_literally():
    """walletlab validates on a notional $100 flat stake. Putting $100 on a
    $40 account would be a 250% position; the FRACTION is what transfers."""
    spec = CopySpec(wallet="0xabc", strategy_id="S", stake_mode="flat",
                    stake_flat=100.0, stake_fraction=0.05)
    assert spec.stake_for(40.0) == pytest.approx(2.0)
    assert spec.stake_for(1000.0) == pytest.approx(50.0)


def test_a_non_matching_market_is_not_a_rejected_signal():
    """Otherwise the entire market universe lands in the ledger as rejections
    and buries the ones that mean something."""
    spec = CopySpec(wallet="0xabc", strategy_id="S", min_price=0.70,
                    max_price=0.98)
    signal, why = signal_from(spec, market="m", token="t", price=0.20,
                              seconds_since_wallet_trade=0, equity=100.0)
    assert signal is None and "floor" in why


def test_a_matching_market_becomes_a_fully_populated_signal():
    spec = CopySpec(wallet="0xabc", strategy_id="S1", score=0.83,
                    min_price=0.5, max_price=0.98, stake_fraction=0.05)
    signal, why = signal_from(spec, market="m1", token="t1", price=0.80,
                              seconds_since_wallet_trade=10, equity=200.0,
                              spread=0.01, liquidity=5000.0, depth=800.0)
    assert why == ""
    assert signal.route == ROUTE_B
    assert signal.behavior_match == 0.83
    assert signal.stake == pytest.approx(10.0)
    assert signal.wallet == "0xabc"
