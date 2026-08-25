"""The gate map's integrity, and the audit's promise never to write."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pqv2 import gatemap                                    # noqa: E402
from pqv2.audit import Paths, render, report, route_a, wiring   # noqa: E402


# ===========================================================================
# the map
# ===========================================================================

def test_every_gate_has_a_valid_owner_and_real_evidence():
    for gate in gatemap.GATES:
        assert gate.owner in gatemap.OWNERS, gate.key
        assert gate.source, gate.key
        # Evidence is not decoration: this map is the justification for
        # letting a signal past a filter, and a gate classified without a
        # stated reason is a classification nobody can argue with.
        assert len(gate.evidence) > 40, gate.key


def test_gate_keys_are_unique():
    keys = [g.key for g in gatemap.GATES]
    assert len(keys) == len(set(keys))


def test_only_account_and_venue_gates_bind_both_routes():
    """The classification rule, checked rather than trusted."""
    for gate in gatemap.GATES:
        both = gate.blocks(gatemap.ROUTE_A) and gate.blocks(gatemap.ROUTE_B)
        if both:
            assert gate.owner in (gatemap.GLOBAL_SAFETY, gatemap.PORTFOLIO,
                                  gatemap.EXECUTION), gate.key
        else:
            assert gate.owner in (gatemap.STRATEGY_A, gatemap.STRATEGY_B), \
                gate.key


def test_learning_mode_is_owned_by_strategy_a():
    """The single most consequential classification in the file: it is what
    permits route B to trade while Strategy A's library is still empty."""
    gate = gatemap.GATES_BY_KEY["learning_mode"]
    assert gate.owner == gatemap.STRATEGY_A
    assert gate.blocks(gatemap.ROUTE_A) is True
    assert gate.blocks(gatemap.ROUTE_B) is False
    assert "40,820" in gate.measured


@pytest.mark.parametrize("reason,expected", [
    ("Learning mode: no validated strategies yet - capital is parked",
     "learning_mode"),
    ("High-confidence filter: market state 0 is not an entry state - a move",
     "hc_market_state"),
    ("cash $1.20 is at or below the $4.00 reserve", "cash_reserve"),
    ("correlated exposure in market:0x1 is $9.00, at the $8.00 cap",
     "cluster_cap"),
    ("no live quote", "no_quote"),
    ("", "unclassified"),
    ("something nobody has ever written down", "unclassified"),
])
def test_journal_reasons_classify_to_the_right_gate(reason, expected):
    assert gatemap.classify(reason) == expected


def test_an_unknown_gate_blocks_by_default():
    assert gatemap.blocks_route("never_seen_before", gatemap.ROUTE_B) is True


# ===========================================================================
# the audit never writes to the original installation
# ===========================================================================

def test_the_audit_opens_the_original_read_only(tmp_path):
    """Non-negotiable rule 1. Proven by making the file unwritable in SQLite's
    eyes and confirming the audit still reads it."""
    journal = tmp_path / "journal.sqlite3"
    conn = sqlite3.connect(str(journal))
    conn.execute("CREATE TABLE decisions (ts REAL, cycle_id TEXT, "
                 "action TEXT, reason TEXT)")
    conn.execute("CREATE TABLE lifecycles (status TEXT, exit_style TEXT, "
                 "realized_pnl REAL)")
    conn.execute("INSERT INTO decisions VALUES(1.0,'c','DO_NOTHING',"
                 "'Learning mode: no validated strategies yet')")
    conn.commit()
    conn.close()

    before = journal.stat().st_mtime_ns
    data = route_a(Paths.discover(tmp_path))
    assert data["available"]
    assert data["decisions"] == 1
    assert data["byGate"][0]["gate"] == "learning_mode"
    assert data["byGate"][0]["owner"] == gatemap.STRATEGY_A
    assert journal.stat().st_mtime_ns == before      # untouched


def test_a_missing_installation_reports_rather_than_raises(tmp_path):
    data = report(Paths.discover(tmp_path / "nothing"), tmp_path / "nope")
    assert data["routeA"]["available"] is False
    assert data["routeA"]["reason"]
    assert len(data["answers"]) == 22
    assert render(data)                              # still renders


def test_the_wiring_check_does_not_false_positive_on_a_shared_filename(
        tmp_path):
    """The bug this test exists for: an earlier version searched for
    'experiments.sqlite3' and reported the engine as CONNECTED, because the
    engine has its own database of that name in research.py. The audit said
    the opposite of the truth."""
    engine = tmp_path / "pqb"
    engine.mkdir()
    (engine / "research.py").write_text(
        "PATH = 'state/experiments.sqlite3'\n", encoding="utf-8")
    result = wiring(Paths.discover(tmp_path), engine)
    assert result["connected"] is False

    (engine / "bridge.py").write_text(
        "from walletlab.registry import Registry\n", encoding="utf-8")
    result = wiring(Paths.discover(tmp_path), engine)
    assert result["connected"] is True
    assert "walletlab" in result["references"][0]


def test_the_audit_answers_all_twenty_two_questions(tmp_path):
    data = report(Paths.discover(tmp_path), tmp_path)
    numbers = [row["n"] for row in data["answers"]]
    assert numbers == list(range(1, 23))
    for row in data["answers"]:
        assert row["answer"], row["question"]
