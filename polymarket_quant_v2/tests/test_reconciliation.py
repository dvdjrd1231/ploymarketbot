"""Regression tests for the reconciliation exit-safety patch.

Section 11 of the patch, plus the isolation guarantees it requires.

The load-bearing test is `test_temporary_snapshot_gap_does_not_close_a_position`
-- it reproduces the exact observed failure and fails against the unpatched
logic, which closes on a single absence.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pqv2.reconciliation import (Diagnostics, ExitReason, PositionEvidence,
                                 ReconciliationGuard, Resolution,
                                 classify_exit, training_filter, verify)

SRC = Path(__file__).resolve().parent.parent / "pqv2"


def held(**kw) -> PositionEvidence:
    base = dict(token_id="tok1", local_size=100.0, local_entry_ts=1000.0,
                last_confirmed_ts=1000.0, snapshot_ts=2000.0,
                snapshot_total_positions=5, source_healthy=True)
    base.update(kw)
    return PositionEvidence(**base)


# --- section 11: the observed failure pattern -------------------------------

def test_temporary_snapshot_gap_does_not_close_a_position():
    """A position exists; one snapshot omits it. NO SELL, NO FORCED CLOSE.

    This is the regression. Against the unpatched `reconcile.py` this scenario
    closes the lifecycle at last known mark with exit_style="reconciled".
    """
    guard = ReconciliationGuard()
    ev = guard.observe(held(snapshot_present=False), now=1.0)

    assert ev.resolution == Resolution.UNCERTAIN.value
    assert not ev.closed_position, "a single absence must never close a position"
    assert ev.exit_reason == "", "no exit label may be assigned"
    assert not ev.training_eligible, "an unresolved event must not teach anything"
    assert guard.diag.to_dict()["reconciliation_exits_prevented"] == 1
    assert guard.pending(), "the position stays under monitoring"


def test_position_reappearing_resolves_as_still_open():
    guard = ReconciliationGuard()
    guard.observe(held(snapshot_present=False), now=1.0)
    ev = guard.observe(held(snapshot_present=True), now=100.0)

    assert ev.resolution == Resolution.POSITION_STILL_OPEN.value
    assert not ev.closed_position
    assert ev.exit_reason == ""
    d = guard.diag.to_dict()
    assert d["reconciliation_position_still_open"] == 1
    assert d["reconciliation_exits_prevented"] == 2


def test_confirmed_disappearance_closes_normally():
    """Then simulate a CONFIRMED disappearance: the position closes normally."""
    guard = ReconciliationGuard()
    ev = guard.observe(held(snapshot_present=False, token_balance=0.0), now=1.0)

    assert ev.closed_position
    assert ev.resolution == Resolution.CONFIRMED_POSITION_CLOSED.value
    assert ev.exit_reason == ExitReason.RECONCILIATION_CONFIRMED.value
    assert ev.training_eligible
    assert guard.diag.to_dict()["reconciliation_exits_confirmed"] == 1


def test_repeated_clean_absence_eventually_confirms():
    """Corroborating absences across separate cycles do become evidence."""
    guard = ReconciliationGuard(required_confirmations=3,
                                min_seconds_between_attempts=10.0)
    t = 0.0
    ev = None
    for _ in range(4):
        t += 60.0
        ev = guard.observe(held(snapshot_present=False), now=t)
    assert ev.closed_position
    assert ev.exit_reason == ExitReason.RECONCILIATION_CONFIRMED.value


def test_rapid_rechecks_cannot_manufacture_confirmation():
    """The same snapshot polled three times in a second is one observation."""
    guard = ReconciliationGuard(required_confirmations=3,
                                min_seconds_between_attempts=30.0)
    ev = None
    for i in range(6):
        ev = guard.observe(held(snapshot_present=False), now=1.0 + i * 0.1)
    assert not ev.closed_position, (
        "rate limiting must stop a fast loop from confirming a closure from "
        "one snapshot")
    assert ev.attempts == 1


# --- the mass-closure defect ------------------------------------------------

def test_empty_snapshot_never_closes_anything():
    """An API blip returning zero positions must not liquidate the book.

    `_actual_positions` returns whatever `exchange_positions()` gives it. If
    that is an empty list, the unpatched loop closes EVERY open position past
    its grace window in a single pass.
    """
    guard = ReconciliationGuard()
    for i in range(20):
        ev = guard.observe(held(token_id=f"tok{i}", snapshot_present=False,
                                snapshot_total_positions=0), now=1.0 + i)
        assert not ev.closed_position
        assert "zero positions in total" in ev.notes[-1]
    assert guard.diag.to_dict()["reconciliation_exits_prevented"] == 20
    assert guard.diag.to_dict()["reconciliation_data_conflicts"] == 20


def test_unhealthy_source_never_closes_anything():
    guard = ReconciliationGuard()
    for i in range(10):
        ev = guard.observe(held(snapshot_present=False, source_healthy=False),
                           now=1.0 + i * 60)
        assert not ev.closed_position


def test_stale_snapshot_cannot_disprove_a_newer_fill():
    ev = verify(held(snapshot_present=False, snapshot_ts=500.0,
                     last_confirmed_ts=900.0))
    assert not ev.may_close
    assert "predates the last confirmed state" in ev.reason


def test_zero_balance_from_a_suspect_source_is_refused():
    v = verify(held(snapshot_present=False, token_balance=0.0,
                    source_healthy=False))
    assert not v.may_close
    assert v.resolution is Resolution.UNCERTAIN


# --- section 5: legitimate exits are untouched ------------------------------

@pytest.mark.parametrize("reason", ["take_profit", "stop", "trailing",
                                    "edge_gone", "settlement", "wallet_exit",
                                    "risk_exit"])
def test_legitimate_strategy_exits_survive_a_nearby_reconciliation(reason):
    """TAKE_PROFIT stays TAKE_PROFIT. STOP stays STOP. SETTLEMENT stays SETTLEMENT."""
    guard = ReconciliationGuard()
    recon = guard.observe(held(snapshot_present=False), now=1.0)
    assert classify_exit(strategy_reason=reason,
                         reconciliation=recon) == ExitReason(reason)


def test_settlement_beats_reconciliation_even_with_no_strategy_reason():
    guard = ReconciliationGuard()
    ev = guard.observe(held(snapshot_present=False, market_resolved=True,
                            resolution_price=1.0), now=1.0)
    assert ev.resolution == Resolution.MARKET_SETTLED.value
    assert ev.exit_reason == ExitReason.SETTLEMENT.value
    assert classify_exit(strategy_reason=None,
                         reconciliation=ev) is ExitReason.SETTLEMENT


def test_a_confirmed_exit_fill_is_not_relabelled_as_a_strategy_exit():
    guard = ReconciliationGuard()
    ev = guard.observe(held(snapshot_present=False, confirmed_exit_fill=True),
                       now=1.0)
    assert ev.exit_reason == ExitReason.RECONCILIATION_CONFIRMED.value
    assert not ExitReason.RECONCILIATION_CONFIRMED.is_strategy_decision


# --- section 7: training-data protection ------------------------------------

def test_unverified_reconciliation_is_quarantined_from_training():
    records = [
        {"id": 1, "exit_reason": "take_profit"},
        {"id": 2, "exit_reason": "stop"},
        {"id": 3, "exit_reason": "reconciliation_unverified"},
        {"id": 4, "exit_reason": "reconciliation_confirmed"},
        {"id": 5, "exit_reason": "settlement"},
    ]
    eligible, quarantined = training_filter(records)
    assert [r["id"] for r in quarantined] == [3]
    assert [r["id"] for r in eligible] == [1, 2, 4, 5]


def test_legacy_reconciled_records_are_quarantined_not_trusted():
    """The unpatched engine wrote exit_style='reconciled'. Those records must
    not silently enter training as if they were decided exits."""
    eligible, quarantined = training_filter([
        {"id": 1, "exit_reason": "reconciled"},
        {"id": 2, "exit_reason": "take_profit"},
    ])
    assert [r["id"] for r in quarantined] == [1]


def test_pending_event_is_never_training_eligible():
    guard = ReconciliationGuard()
    ev = guard.observe(held(snapshot_present=False), now=1.0)
    assert not ev.training_eligible


def test_exit_reason_taxonomy_separates_decisions_from_bookkeeping():
    assert ExitReason.TAKE_PROFIT.is_strategy_decision
    assert ExitReason.STOP.is_strategy_decision
    assert not ExitReason.SETTLEMENT.is_strategy_decision
    assert not ExitReason.RECONCILIATION_CONFIRMED.is_strategy_decision
    assert not ExitReason.RECONCILIATION_UNVERIFIED.training_eligible


# --- section 8: the raw event is preserved ----------------------------------

def test_raw_event_is_preserved_alongside_its_resolution():
    guard = ReconciliationGuard()
    guard.observe(held(snapshot_present=False), now=1.0)
    ev = guard.observe(held(snapshot_present=True), now=100.0)
    assert ev.raw == "RECONCILIATION_EVENT"
    assert ev.resolution == Resolution.POSITION_STILL_OPEN.value
    assert ev.expected["size"] == 100.0
    assert len(ev.notes) >= 2, "the full attempt trail is kept"
    assert guard.history and guard.history[0] is ev


# --- section 4 / 14: bounded, fail-safe -------------------------------------

def test_retries_are_bounded_and_abandon_to_monitoring_not_to_closure():
    guard = ReconciliationGuard(required_confirmations=99, max_attempts=4,
                                min_seconds_between_attempts=1.0)
    ev = None
    t = 0.0
    for _ in range(10):
        t += 60.0
        ev = guard.observe(held(snapshot_present=False,
                                snapshot_total_positions=0), now=t)
    assert ev.resolution == Resolution.ABANDONED.value
    assert not ev.closed_position, "a timeout is not evidence of closure"
    assert "REMAINS OPEN" in ev.notes[-1]
    assert guard.diag.to_dict()["reconciliation_abandoned_to_monitoring"] == 1


def test_abandoned_events_are_not_re_opened_forever():
    """Regression: abandoning to monitoring must not restart the budget.

    An earlier revision popped the abandoned event and then rebuilt a fresh one
    on the next silent snapshot, so `attempts` reset every cycle and the guard
    churned indefinitely -- the unbounded loop section 4 forbids.
    """
    guard = ReconciliationGuard(required_confirmations=99, max_attempts=3,
                                min_seconds_between_attempts=1.0)
    t = 0.0
    for _ in range(50):
        t += 60.0
        ev = guard.observe(held(snapshot_present=False,
                                snapshot_total_positions=0), now=t)
    assert ev.resolution == Resolution.ABANDONED.value
    assert not ev.closed_position
    # Exactly one event was ever opened for this token.
    assert guard.diag.to_dict()["reconciliation_events"] == 1
    assert len(guard.history) == 1


def test_abandoned_event_is_revived_by_authoritative_evidence():
    guard = ReconciliationGuard(required_confirmations=99, max_attempts=2,
                                min_seconds_between_attempts=1.0)
    t = 0.0
    for _ in range(6):
        t += 60.0
        guard.observe(held(snapshot_present=False,
                           snapshot_total_positions=0), now=t)
    ev = guard.observe(held(snapshot_present=True), now=t + 60)
    assert ev.resolution == Resolution.POSITION_STILL_OPEN.value
    assert any("revived" in n for n in ev.notes)


def test_no_observation_at_all_fails_safe_to_uncertain():
    v = verify(PositionEvidence(token_id="t", local_size=10.0))
    assert not v.may_close
    assert v.resolution is Resolution.UNCERTAIN


def test_guard_refuses_a_single_confirmation_configuration():
    with pytest.raises(ValueError, match="at least two"):
        ReconciliationGuard(required_confirmations=1)


def test_verify_has_no_path_that_closes_without_explicit_evidence():
    """Exhaustive: only the four authorised evidence types may close."""
    import itertools
    closing = []
    for present, balance, resolved, fill in itertools.product(
            [None, True, False], [None, 0.0, 5.0], [None, True, False],
            [None, True, False]):
        v = verify(held(snapshot_present=present, token_balance=balance,
                        market_resolved=resolved, confirmed_exit_fill=fill))
        if v.may_close:
            closing.append((present, balance, resolved, fill))
    assert closing, "sanity: some combinations must be closable"
    for present, balance, resolved, fill in closing:
        assert resolved is True or fill is True or balance == 0.0, (
            f"closed with no authoritative evidence: present={present} "
            f"balance={balance} resolved={resolved} fill={fill}")


# --- sections 6 / 13 / 15: isolation ----------------------------------------

def _imports_of(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def test_reconciliation_module_touches_nothing_else():
    """Section 6/13: an exit-state patch must not reach entry, sizing,
    compounding, wallet or strategy code."""
    mods = _imports_of(SRC / "reconciliation.py")
    forbidden = [m for m in mods
                 if any(x in m for x in ("strategy_a", "strategy_b", "risk",
                                         "validation", "research", "substrate",
                                         "accel", "config", "gates",
                                         "ledger"))]
    assert not forbidden, (
        f"the reconciliation patch imports unrelated subsystems: {forbidden}")


def test_no_strategy_or_risk_module_depends_on_this_patch():
    """Section 15: the patch must not alter Strategy A or Strategy B behaviour.

    Only PRESENTATION may import it -- the CLI and its own report renderer.
    If a strategy, risk, validation or research module ever imports it, the
    patch has stopped being surgical and has become a cross-cutting change to
    decision-making code.
    """
    allowed = {"cli.py", "reconciliation_report.py"}
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name in allowed | {"reconciliation.py"}:
            continue
        if any("reconciliation" in m for m in _imports_of(path)):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, (
        f"decision-making modules now depend on the exit patch: {offenders}")


def test_strategy_b_engine_is_untouched_by_the_patch():
    """The upcoming wallet/RN1 route must be behaviourally unchanged."""
    engine = _imports_of(SRC / "strategy_b" / "engine.py")
    assert not any("reconciliation" in m for m in engine)
    for name in ("sizing.py", "compounding.py", "portfolio.py", "execution.py"):
        assert not any("reconciliation" in m
                       for m in _imports_of(SRC / "risk" / name))


def test_diagnostics_never_influence_decisions():
    """Section 9: counters are diagnostics only."""
    src = (SRC / "reconciliation.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "verify":
            body = ast.dump(node)
            assert "diag" not in body and "counters" not in body, (
                "verify() reads diagnostic counters; they must not influence "
                "any decision")
