"""The research layer reaching the screen, driven through the real dashboard.

The layer wrote seven new fields into `strategies.json` and the dashboard
showed none of them, which for a GUI-first operator is the same as the layer
not existing. These tests render the real Discovery tab over real strategy
records and assert on the cell text — not on the reader, not on a helper, on
what the operator actually sees.

The load-bearing assertion is the last one: an attacked candidate's STATUS
must be untouched by its verdict. If a BROKEN row ever renders as rejected,
the adversarial layer has acquired authority it is explicitly denied.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6", reason="dashboard tests need PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from pqb.config import load  # noqa: E402
from pqb.gui.app import Dashboard, _attack_cell  # noqa: E402

SRC_CONFIG = (Path(__file__).resolve().parents[1]
              / "config" / "config.example.yaml")

HEADERS = ["Trading rule", "Ver", "Status", "Why not trading", "Attacked",
           "Why researching", "Priority"]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _strategy(**over) -> dict:
    base = {
        "signature": "seq|price_down_impulse|spread_widening|up",
        "describe": "chain: price down impulse -> spread widening, long",
        "rule": {"type": "sequence"}, "status": "validating", "version": 1,
        "oosTrades": 12, "oosMarkets": 4, "oosWin": 0.5,
        "oosExpectancy": 0.0103, "evidence": 0.21, "priority": 0.74,
        "blockers": ["OOS_MARKET_BREADTH"], "nextAction": "allocate markets",
        "adversarialVerdict": "", "robustness": 0.0,
        "adversarialCoverage": 0.0, "adversarialFailed": [],
        "researchReward": 0.0, "whyMoreResearch": "", "whyStopped": "",
    }
    base.update(over)
    return base


@pytest.fixture
def dash(qapp, tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "config.yaml"
    shutil.copy(SRC_CONFIG, cfg_path)
    cfg = load(cfg_path)
    window = Dashboard(cfg, cfg_path)
    yield window
    window.bot.request_stop()
    window.close()


def _render(dash, strategies: list[dict], monkeypatch) -> list[list[str]]:
    """Fill the real Discovery tab and read the cell text back out."""
    monkeypatch.setattr(
        dash.reader, "discovery",
        lambda: {"strategies": strategies, "status": {}, "tokensReady": 0})
    dash._fill_discovery()
    table = dash.discovery_table
    return [[(table.item(r, c).text() if table.item(r, c) else "")
             for c in range(table.columnCount())]
            for r in range(table.rowCount())]


def _column(dash, name: str) -> int:
    header = dash.discovery_table.horizontalHeader()
    labels = [dash.discovery_table.horizontalHeaderItem(i).text()
              for i in range(header.count())]
    return labels.index(name)


# -- the cell itself ----------------------------------------------------------

def test_a_survival_never_renders_without_its_coverage():
    """A SURVIVED at 40% coverage dodged most of the battery. Rendering it as
    a bare "SURVIVED" is the manufactured confidence this layer exists to
    withhold, so the percentage is not optional."""
    cell = _attack_cell(_strategy(adversarialVerdict="SURVIVED",
                                  robustness=0.91,
                                  adversarialCoverage=0.4))
    assert "SURVIVED" in cell
    assert "40%" in cell


def test_an_unattacked_candidate_says_so_rather_than_showing_blank():
    """An empty cell reads as missing data. "not attacked" is a real state:
    the battery declines records too thin to attack."""
    assert _attack_cell(_strategy()) == "not attacked"
    assert _attack_cell(
        _strategy(adversarialVerdict="NOT_ATTACKED")) == "not attacked"


def test_the_failed_tests_are_named_in_the_cell():
    cell = _attack_cell(_strategy(
        adversarialVerdict="BROKEN", robustness=0.37,
        adversarialCoverage=0.6,
        adversarialFailed=["concentration", "drawdown_stress"]))
    assert "BROKEN" in cell and "0.37" in cell
    assert "concentration" in cell


# -- through the real table ---------------------------------------------------

def test_the_discovery_tab_shows_the_attack_and_the_reason(dash, monkeypatch):
    rows = _render(dash, [_strategy(
        adversarialVerdict="BROKEN", robustness=0.37,
        adversarialCoverage=0.6,
        adversarialFailed=["concentration", "leave_one_market_out"],
        whyMoreResearch="positive unseen expectancy (+0.0103); also 4 "
                        "independent OOS markets")], monkeypatch)
    assert len(rows) == 1
    attacked = rows[0][_column(dash, "Attacked")]
    why = rows[0][_column(dash, "Why researching")]
    assert "BROKEN" in attacked and "concentration" in attacked
    assert "independent OOS markets" in why


def test_a_stopped_candidate_explains_why_it_stopped(dash, monkeypatch):
    """§13 asks for both questions answered. One column serves both, because
    a candidate is either being researched or it is not."""
    rows = _render(dash, [_strategy(
        whyStopped="deprioritised: independent candidates in this family "
                   "have repeatedly failed the same way")], monkeypatch)
    assert "repeatedly failed" in rows[0][_column(dash, "Why researching")]


def test_a_broken_candidate_keeps_its_status_and_its_record(dash, monkeypatch):
    """THE architectural assertion. The adversarial layer may lower a
    research priority and nothing else. If BROKEN ever renders as a rejected
    or retired row, the battery has taken authority the ladder owns — and the
    separation §17 depends on has been lost in the presentation layer, which
    is where nobody would look for it."""
    rows = _render(dash, [_strategy(
        status="validating", adversarialVerdict="BROKEN", robustness=0.37,
        adversarialCoverage=0.6, adversarialFailed=["concentration"])],
        monkeypatch)
    assert rows[0][_column(dash, "Status")] == "validating"
    # ...and the evidence columns are untouched by the verdict.
    assert rows[0][_column(dash, "Trading rule")].startswith("chain:")
    assert "12" in rows[0]


def test_a_validated_row_that_survived_still_reads_as_validated(dash,
                                                                monkeypatch):
    rows = _render(dash, [_strategy(
        status="validated", adversarialVerdict="SURVIVED", robustness=0.95,
        adversarialCoverage=0.8)], monkeypatch)
    assert rows[0][_column(dash, "Status")] == "VALIDATED"
    assert "SURVIVED" in rows[0][_column(dash, "Attacked")]


def test_the_panel_counts_what_was_attacked_and_what_broke(dash, monkeypatch):
    _render(dash, [
        _strategy(adversarialVerdict="BROKEN", adversarialCoverage=0.6),
        _strategy(adversarialVerdict="SURVIVED", adversarialCoverage=0.7),
        _strategy(),
    ], monkeypatch)
    note = dash.discovery_note.text()
    assert "2 candidate(s) have been deliberately attacked" in note
    assert "1 broke" in note
    # And it says plainly that none of it can promote anything.
    assert "can promote or reject" in note.replace("</b>", "")


def test_the_tab_survives_records_written_before_this_layer_existed(
        dash, monkeypatch):
    """Every field here is new. A library row from an older pass has none of
    them, and the tab must render rather than raise — the dashboard reads
    files it did not write."""
    old = {"signature": "seq|old", "describe": "an older record",
           "status": "new", "version": 1}
    rows = _render(dash, [old], monkeypatch)
    assert rows[0][_column(dash, "Attacked")] == "not attacked"
    assert rows[0][_column(dash, "Why researching")] == "-"


def test_every_strategy_json_field_the_layer_writes_has_a_home(dash,
                                                               monkeypatch):
    """A guard against the failure this file was written to fix: the layer
    adding a field that nothing on screen ever shows."""
    shown = {"adversarialVerdict", "robustness", "adversarialCoverage",
             "adversarialFailed", "whyMoreResearch", "whyStopped"}
    rows = _render(dash, [_strategy(
        adversarialVerdict="BROKEN", robustness=0.37,
        adversarialCoverage=0.6, adversarialFailed=["concentration"],
        whyMoreResearch="because it is informative")], monkeypatch)
    rendered = " ".join(rows[0])
    assert "BROKEN" in rendered            # adversarialVerdict
    assert "0.37" in rendered              # robustness
    assert "60%" in rendered               # adversarialCoverage
    assert "concentration" in rendered     # adversarialFailed
    assert "informative" in rendered       # whyMoreResearch
    assert shown                           # documents the contract above


def test_the_explanation_can_be_resized_and_collapsed_off_the_tab(dash):
    """The prose above the table is long and grows with each pass. On a laptop
    it pushed the rows off the bottom with no way to reach them, so the tab is
    a splitter: drag the divider, or collapse the prose entirely and give the
    whole tab to the table.

    The restore assertion is the point of the test. Coming back at the default
    height rather than the height the operator dragged to would silently undo
    their layout every time they peeked at the rows.
    """
    assert dash.discovery_split.count() == 2
    assert dash.discovery_split.widget(1) is dash.discovery_table

    dash.discovery_split.setSizes([120, 560])
    dragged = dash.discovery_split.sizes()[0]

    dash.discovery_toggle.setChecked(False)
    dash._toggle_discovery_note()
    assert dash.discovery_scroll.isHidden()
    assert "Show" in dash.discovery_toggle.text()

    dash.discovery_toggle.setChecked(True)
    dash._toggle_discovery_note()
    assert not dash.discovery_scroll.isHidden()
    assert dash.discovery_split.sizes()[0] == dragged
    assert "Hide" in dash.discovery_toggle.text()
