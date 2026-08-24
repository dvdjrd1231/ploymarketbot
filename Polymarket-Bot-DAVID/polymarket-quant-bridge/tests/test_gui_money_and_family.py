"""The two new dashboard surfaces, driven against real databases.

Both panes exist to say something a person can act on, and both are showing
numbers that must never be read as permission. So the assertions here are as
much about the WORDS as the columns: a family score next to an OOS trade count
is only safe if the panel says which one can validate anything, and a
counterfactual column is only safe if it says it is not evidence.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6", reason="dashboard tests need PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from pqb.config import load  # noqa: E402
from pqb.gui.app import Dashboard  # noqa: E402
from pqb.journal import _SCHEMA as JOURNAL_SCHEMA  # noqa: E402

SRC_CONFIG = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"
BASE = 1_700_000_000.0


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def project(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "config.yaml"
    shutil.copy(SRC_CONFIG, cfg_path)
    cfg = load(cfg_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg, cfg_path


def _seed_journal(cfg, trades=12):
    conn = sqlite3.connect(cfg.journal_path)
    conn.executescript(JOURNAL_SCHEMA)
    for i in range(1, trades + 1):
        ts = BASE + i * 3_600
        conn.execute(
            "INSERT INTO decisions(id, ts, action, market_id, token_id, "
            "outcome, question, score, reason) VALUES(?,?,?,?,?,?,?,?,?)",
            (900 + i, ts, "BUY", f"mk{i % 3}", f"tk{i}", f"out{i}",
             f"question {i}", 0.7, "entered"))
        pnl = -0.8 if i % 3 else +1.6
        conn.execute(
            "INSERT INTO lifecycles(token_id, market_id, outcome, question, "
            "status, entry_decision_id, entry_ts, entry_price, entry_size, "
            "entry_cost, peak_price, trough_price, exit_ts, exit_price, "
            "exit_size, exit_reason, exit_style, realized_pnl, return_pct, "
            "hold_seconds, category, liquidity_bucket, ttr_bucket, mode) "
            "VALUES(?,?,?,?,'CLOSED',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"tk{i}", f"mk{i % 3}", f"out{i}", f"question {i}", 900 + i, ts,
             0.5, 10.0, 5.0, 0.55, 0.45, ts + 900, 0.42, 10.0, "because",
             ["stop", "edge_gone", "wallet"][i % 3], pnl, pnl / 5.0, 900.0,
             "sports", "deep", "hours", "dry_run"))
    # One open position, and a second in the SAME market so the correlated
    # column has something true to say.
    for token, market in (("open1", "mkA"), ("open2", "mkA")):
        conn.execute(
            "INSERT INTO lifecycles(token_id, market_id, outcome, question, "
            "status, entry_ts, entry_price, entry_size, entry_cost, "
            "peak_price, trough_price, mode) "
            "VALUES(?,?,?,?,'OPEN',?,?,?,?,?,?,?)",
            (token, market, f"o-{token}", "an open question", BASE, 0.40,
             10.0, 4.0, 0.52, 0.31, "dry_run"))
    conn.execute(
        "INSERT INTO cycles(cycle_id, ts, markets, positions, "
        "portfolio_value, balance) VALUES(?,?,?,?,?,?)",
        ("c1", BASE + 100_000, 25, 2, 84.0, 40.0))
    conn.commit()
    conn.close()


def _seed_strategies(cfg):
    (cfg.data_dir / "strategies.json").write_text(json.dumps({"strategies": [{
        "signature": "sig#v3", "describe": "SHORT when flow_z_z > 2",
        "status": "validating", "version": 3, "oosTrades": 4,
        "oosMarkets": 1, "oosExpectancy": -0.01, "evidence": 0.02,
        "priority": 0.64, "blockers": ["OOS_TRADES 4/30"],
        "familyTrades": 330, "familyMarkets": 83, "familyVersions": 5,
        "familyExpectancy": 0.42,
        "motif": "family=mean-reversion", "motifWeight": 1.31,
        "familyResearchScore": 0.58, "familyReplication": 0.8,
        "familyIndependentMarkets": 23, "familyIndependentCandidates": 7,
        "familyFailureMotif": "",
        "whyFamilyElevated": "elevated because 7 independent candidate(s)...",
        "whyFamilyDeprioritised": "",
    }]}), encoding="utf-8")
    (cfg.data_dir / "motifs.json").write_text(json.dumps({
        "motifs": [], "scale": {"motifsExamined": 412,
                                "motifsWithStanding": 9,
                                "motifsReplicated": 2, "motifFailures": 5,
                                "motifStrongest": "family=mean-reversion"}}),
        encoding="utf-8")


# -- the family pane ---------------------------------------------------------


def test_the_discovery_board_shows_the_motif_and_family_score(qapp, project):
    cfg, cfg_path = project
    _seed_strategies(cfg)
    window = Dashboard(cfg, cfg_path)
    try:
        window._fill_discovery()
        headers = [window.discovery_table.horizontalHeaderItem(c).text()
                   for c in range(window.discovery_table.columnCount())]
        assert "Motif" in headers
        assert "Family score" in headers
        row = {h: window.discovery_table.item(0, c).text()
               for c, h in enumerate(headers)}
        assert row["Motif"] == "mean-reversion"
        # The independent-confirmation count, never the raw candidate count.
        assert "7ic" in row["Family score"]
        # The OOS columns are untouched by any of it.
        assert row["OOS trades"] == "4"
        assert row["Status"] == "validating"
    finally:
        window.close()


def test_the_board_explains_that_the_family_layer_cannot_promote(qapp, project):
    cfg, cfg_path = project
    _seed_strategies(cfg)
    window = Dashboard(cfg, cfg_path)
    try:
        window._fill_discovery()
        note = window.discovery_note.text()
        assert "cannot promote" in note or "unable to promote" in note
        assert "ONE confirmation" in note
        assert "never lends a single trade" in note
        # The scale of the search is stated next to the finding.
        assert "412" in note
    finally:
        window.close()


def test_the_drilldown_separates_candidate_evidence_from_family_evidence(
        qapp, project, monkeypatch):
    cfg, cfg_path = project
    _seed_strategies(cfg)
    from PyQt6.QtWidgets import QMessageBox

    captured: list[str] = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: captured.append(
        box.text()) or 0)
    window = Dashboard(cfg, cfg_path)
    try:
        window._fill_discovery()
        window._show_family_detail(0, 0)
        assert captured
        panel = captured[0]
        assert "THIS CANDIDATE'S OWN EVIDENCE" in panel
        assert "the only thing that can ever validate it" in panel
        assert "RESEARCH EVIDENCE ONLY" in panel
        assert "cannot change the status" in panel
        assert "counted once" in panel
    finally:
        window.close()


def test_the_drilldown_is_safe_on_an_empty_board(qapp, project):
    cfg, cfg_path = project
    window = Dashboard(cfg, cfg_path)
    try:
        window._fill_discovery()
        window._show_family_detail(0, 0)      # must not raise
        window._show_family_detail(99, 0)
    finally:
        window.close()


# -- the money-management pane ----------------------------------------------


def test_results_shows_the_money_management_diagnostics(qapp, project):
    cfg, cfg_path = project
    _seed_journal(cfg)
    window = Dashboard(cfg, cfg_path)
    try:
        window._fill_results()
        note = window.mm_note.text()
        assert "MONEY MANAGEMENT DIAGNOSTICS" in note
        assert "Hurting the equity curve most" in note
        assert "Saving it most" in note
        assert "nothing on it can change a stop" in note
        assert window.mm_table.rowCount() > 0
    finally:
        window.close()


def test_a_thin_sample_recommends_no_change(qapp, project):
    cfg, cfg_path = project
    _seed_journal(cfg, trades=12)
    window = Dashboard(cfg, cfg_path)
    try:
        window._fill_results()
        assert "Recommended changes: none" in window.mm_note.text()
    finally:
        window.close()


def test_closed_trades_label_the_counterfactual_as_not_evidence(qapp, project):
    cfg, cfg_path = project
    _seed_journal(cfg)
    window = Dashboard(cfg, cfg_path)
    try:
        window._fill_tables()
        headers = [window._tbl_clos.horizontalHeaderItem(c).text()
                   for c in range(window._tbl_clos.columnCount())]
        assert "If held 30m longer" in headers
        note = window._note_clos.text()
        assert "COUNTERFACTUAL" in note
        assert "never counted as evidence" in note
        assert "no rule is changed because of it" in note
    finally:
        window.close()


def test_open_positions_show_correlated_exposure(qapp, project):
    cfg, cfg_path = project
    _seed_journal(cfg)
    window = Dashboard(cfg, cfg_path)
    try:
        window._fill_tables()
        headers = [window._tbl_open.horizontalHeaderItem(c).text()
                   for c in range(window._tbl_open.columnCount())]
        assert "Correlated with" in headers
        column = headers.index("Correlated with")
        texts = [window._tbl_open.item(r, column).text()
                 for r in range(window._tbl_open.rowCount())]
        assert any("other position(s) in this market" in t for t in texts)
        assert "one bet held twice" in window._note_open.text()
        assert "Nothing here forces an exit" in window._note_open.text()
    finally:
        window.close()


def test_the_dashboard_survives_an_empty_project(qapp, project):
    cfg, cfg_path = project
    window = Dashboard(cfg, cfg_path)
    try:
        window._fill_results()
        window._fill_tables()
        window._fill_discovery()
        assert "nothing to diagnose yet" in window.mm_note.text()
    finally:
        window.close()
