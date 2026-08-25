"""Non-negotiable rule 4: Strategy B is never silently blocked by Strategy A.

These are the tests that would fail if someone "helpfully" reused a V1 filter
inside the Strategy B route. They assert structure, not behaviour, because
behaviour can be correct by accident and structure cannot.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pqv2 import gates
from pqv2.gates import Owner
from pqv2.ledger import Funnel, SignalRecord, Stage

SRC = Path(__file__).resolve().parent.parent / "pqv2"


def test_strategy_a_gate_cannot_block_route_b():
    rec = SignalRecord(signal_id="X", ts=0, route="B")
    with pytest.raises(AssertionError, match="must not be evaluated on route"):
        rec.reject(Stage.STRATEGY_REJECTED, "v1.learning_mode", "nope")


def test_strategy_b_gate_cannot_block_route_a():
    rec = SignalRecord(signal_id="X", ts=0, route="A")
    with pytest.raises(AssertionError):
        rec.reject(Stage.STRATEGY_REJECTED, "b.behavior_match", "nope")


def test_global_and_portfolio_gates_bind_both_routes():
    for key in ("g.price_bounds", "g.drawdown_halt", "p.max_open",
                "x.unpriced"):
        for route in ("A", "B"):
            gates.assert_may_block(key, route)      # must not raise


def test_every_global_safety_gate_carries_written_evidence():
    """A GLOBAL_SAFETY gate with no evidence is a Strategy A gate in disguise.

    This is the loophole that would let anyone re-impose V1's filters on both
    routes just by labelling them 'safety'.
    """
    unjustified = gates.audit()["unjustified_global"]
    assert not unjustified, (
        f"these global gates block both strategies with no stated evidence: "
        f"{unjustified}")


def test_unregistered_gate_is_refused():
    rec = SignalRecord(signal_id="X", ts=0, route="B")
    with pytest.raises(KeyError, match="unregistered gate"):
        rec.advance(Stage.STRATEGY_REJECTED, gate_key="made.up",
                    reason="invented")


def test_rejection_without_a_gate_key_is_refused():
    """Rule 6: always log the exact rejection reason."""
    rec = SignalRecord(signal_id="X", ts=0, route="B")
    with pytest.raises(AssertionError, match="no\\s+gate key"):
        rec.advance(Stage.STRATEGY_REJECTED, reason="because")


def test_terminal_state_is_final():
    rec = SignalRecord(signal_id="X", ts=0, route="B")
    rec.reject(Stage.STRATEGY_REJECTED, "b.conditions", "no")
    with pytest.raises(AssertionError, match="already terminal"):
        rec.advance(Stage.RISK_PASSED)


def test_signal_cannot_skip_forward():
    rec = SignalRecord(signal_id="X", ts=0, route="B")
    rec.advance(Stage.BEHAVIOR_MATCHED)
    rec.advance(Stage.STRATEGY_ACCEPTED)
    with pytest.raises(AssertionError, match="not forward progress"):
        rec.advance(Stage.BEHAVIOR_MATCHED)


def test_funnel_refuses_to_close_when_signals_vanish():
    """The whole point of the ledger: an unexplained gap must raise."""
    f = Funnel()
    rec = SignalRecord(signal_id="X", ts=0, route="B")
    f.record(rec)
    f.assert_balanced()                     # in flight is fine
    f.stages["B"][Stage.STRATEGY_REJECTED.value] += 5      # forge a gap
    with pytest.raises(AssertionError, match="unexplained gap"):
        f.assert_balanced()


# --- structural: the import graph -----------------------------------------

def _imports_of(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out.add("." * (node.level or 0) + node.module)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def test_strategy_b_never_imports_strategy_a():
    """Structural proof that the routes are independent."""
    for path in (SRC / "strategy_b").glob("*.py"):
        mods = _imports_of(path)
        offenders = [m for m in mods if "strategy_a" in m]
        assert not offenders, f"{path.name} imports Strategy A: {offenders}"


def test_ai_is_not_importable_from_the_execution_loop():
    """The AI research module must never sit in the hot path."""
    for name in ("engine.py", "strategy.py", "behavior.py"):
        mods = _imports_of(SRC / "strategy_b" / name)
        assert not [m for m in mods if m.endswith("ai") or ".ai" in m], (
            f"{name} imports the AI module; it must stay out of the execution "
            "loop")
    for name in ("sizing.py", "portfolio.py", "execution.py"):
        mods = _imports_of(SRC / "risk" / name)
        assert not [m for m in mods if m.endswith("ai") or ".ai" in m]


def test_only_the_ladder_assigns_status():
    """Nothing outside validation/validate.py may set a strategy status.

    If a future change makes a strategy validate because an AI liked it, or
    because it survived an attack, or because it resembles RN1, this fails.
    """
    allowed = SRC / "validation" / "validate.py"
    for path in SRC.rglob("*.py"):
        if path == allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name != "assign_status", (
                    f"{path.relative_to(SRC)} calls assign_status; the ladder "
                    "is the only authority on status")


def test_v2_never_writes_to_the_v1_installation():
    """Every database V2 opens for writing must be under work_dir.

    Non-negotiable rules 1-3: never overwrite, never delete, never modify the
    original program.
    """
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "attr", None)
            if name != "connect":
                continue
            mod = getattr(getattr(fn, "value", None), "id", None)
            if mod != "sqlite3":
                continue
            # A raw sqlite3.connect is only permitted with mode=ro, or inside
            # the two modules that own V2's own databases.
            src = ast.get_source_segment(text, node) or ""
            owns_v2_db = path.name in ("ledger.py", "registry.py")
            if "mode=ro" not in src and not owns_v2_db:
                offenders.append(f"{path.relative_to(SRC)}: {src[:70]}")
    assert not offenders, (
        "these open a database for writing outside V2's own stores: "
        + "; ".join(offenders))
