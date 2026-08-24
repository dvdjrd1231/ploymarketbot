"""Driving the REAL dashboard, offscreen: every button path a user can take.

The engine had deep test coverage while the GUI's interactive flows were
verified by reasoning — and that is precisely where every operator-facing
failure came from (silent start errors, dead Stop for detached bots, reset
against a locked journal). These tests construct the actual Dashboard, click
the actual handlers, and auto-answer the actual dialogs, so those paths live
in the suite instead of in the operator's lap.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6", reason="dashboard tests need PyQt6")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from pqb.config import load  # noqa: E402
from pqb.gui.app import Dashboard  # noqa: E402

SRC_CONFIG = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def dash(qapp, tmp_path):
    """A real Dashboard over a throwaway project directory."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "config.yaml"
    shutil.copy(SRC_CONFIG, cfg_path)
    cfg = load(cfg_path)
    window = Dashboard(cfg, cfg_path)
    yield window
    window.bot.request_stop()
    window.close()


class _AutoDialog:
    """Auto-answers QMessageBox dialogs by button index and records calls."""

    def __init__(self, monkeypatch, pick_index: int):
        self.informations: list[tuple] = []
        self.warnings: list[tuple] = []

        def fake_exec(box):
            made = getattr(box, "_made_buttons", [])
            box._picked = made[pick_index] if made else None
            return 0

        original_add = QMessageBox.addButton

        def spy_add(box, *args, **kwargs):
            button = original_add(box, *args, **kwargs)
            box.__dict__.setdefault("_made_buttons", []).append(button)
            return button

        monkeypatch.setattr(QMessageBox, "exec", fake_exec)
        monkeypatch.setattr(QMessageBox, "addButton", spy_add)
        monkeypatch.setattr(QMessageBox, "clickedButton",
                            lambda box: getattr(box, "_picked", None))
        monkeypatch.setattr(
            QMessageBox, "information",
            classmethod(lambda cls, *a, **k: self.informations.append(a)))
        monkeypatch.setattr(
            QMessageBox, "warning",
            classmethod(lambda cls, *a, **k: self.warnings.append(a)))


# -- refresh and cold start ---------------------------------------------------

def test_dashboard_builds_and_refreshes_on_a_fresh_install(dash):
    dash.refresh()                      # empty state: must not raise
    assert dash.tabs.count() >= 8


def test_failed_start_reason_reaches_the_banner(dash, tmp_path, monkeypatch):
    """The silent-start-failure hole, as a test: a config the validator
    rejects must surface its exact words on the Overview."""
    # The throwaway project root has no pqb package (the real install does),
    # so point the child at this repo explicitly.
    monkeypatch.setenv("PYTHONPATH",
                       str(Path(__file__).resolve().parents[1]))
    cfg_path = dash.config_path
    text = cfg_path.read_text(encoding="utf-8")
    cfg_path.write_text(text.replace("cycle_seconds: 2",
                                     "cycle_seconds: 0"), encoding="utf-8")
    dash.bot.start()
    deadline = __import__("time").time() + 20
    while dash.bot.alive and __import__("time").time() < deadline:
        __import__("time").sleep(0.2)
    assert not dash.bot.alive, "bot should die on the invalid config"
    failure = dash.bot.start_error()
    assert "cycle_seconds" in failure


# -- panic / resume -----------------------------------------------------------

def test_panic_halt_writes_kill_and_resume_clears_it(dash, monkeypatch):
    _AutoDialog(monkeypatch, pick_index=0)          # "Stop new orders"
    dash.on_panic()
    assert dash.cfg.kill_switch_path.exists()
    assert "halt" in dash.cfg.kill_switch_path.read_text(encoding="utf-8")

    dash._last_o = {"open_positions": 0, "running": False}
    dash.on_resume()
    assert not dash.cfg.kill_switch_path.exists()


def test_panic_flatten_with_bot_off_explains_the_missing_step(dash, monkeypatch):
    dialogs = _AutoDialog(monkeypatch, pick_index=1)   # "Stop and close"
    dash._last_o = {"running": False}
    dash.on_panic()
    assert dash.cfg.kill_switch_path.exists()
    assert dialogs.informations, "must explain the bot is off"


def test_resume_mid_closeout_offers_to_keep_closing(dash, monkeypatch):
    dash.cfg.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
    dash.cfg.kill_switch_path.write_text("flatten", encoding="utf-8")
    dash._last_o = {"open_positions": 3, "running": True}
    _AutoDialog(monkeypatch, pick_index=0)          # "Keep closing"
    dash.on_resume()
    assert dash.cfg.kill_switch_path.exists(), "keep-closing must not clear it"


# -- reset --------------------------------------------------------------------

def test_reset_refuses_while_any_bot_runs_even_detached(dash, monkeypatch):
    """The operator's journal.sqlite3 lock: a bot from ANOTHER window shows in
    the heartbeat, not in self.bot — the guard must catch it BEFORE the lock."""
    dialogs = _AutoDialog(monkeypatch, pick_index=0)
    dash._last_o = {"running": True, "open_positions": 0}
    dash.on_reset()
    assert dialogs.informations, "must tell the operator to press Stop first"
    assert "Stop" in dialogs.informations[0][2]


def test_reset_wipes_and_dashboard_survives(dash, monkeypatch):
    journal = dash.cfg.journal_path
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("old session", encoding="utf-8")
    dash._last_o = {"running": False, "open_positions": 0}
    _AutoDialog(monkeypatch, pick_index=0)          # "Reset to $100"
    dash.on_reset()
    assert not journal.exists()
    dash.refresh()                                  # fresh state renders


# -- settings round trip ------------------------------------------------------

def test_settings_save_and_reload_through_the_real_widgets(dash):
    dash.load_settings()
    spec, field = dash.setting_widgets["engine.portfolio.max_position_fraction"]
    field.setValue(12.5)                            # 12.5%
    dash.save_settings()
    cfg = load(dash.config_path)
    assert abs(cfg.engine.portfolio.max_position_fraction - 0.125) < 1e-9
    dash.load_settings()
    assert abs(field.value() - 12.5) < 1e-6


def test_toggled_setting_off_writes_zero_via_widgets(dash):
    dash.load_settings()
    toggle, _default = dash.setting_toggles[
        "engine.portfolio.reserve_cash_fraction"]
    toggle.setChecked(False)
    dash.save_settings()
    cfg = load(dash.config_path)
    assert cfg.engine.portfolio.reserve_cash_fraction == 0.0
    dash.load_settings()
    assert not toggle.isChecked()


def test_bool_setting_roundtrips_via_widgets(dash):
    dash.load_settings()
    spec, check = dash.setting_widgets["engine.exits.stagnation_enabled"]
    check.setChecked(True)
    dash.save_settings()
    assert load(dash.config_path).engine.exits.stagnation_enabled is True
    dash.load_settings()
    assert check.isChecked()


def test_the_results_tab_renders_the_consistency_block(dash):
    """§26 of the consistency patch, in the real widget.

    A panel that raises on a fresh install takes the whole Results tab down
    with it, and the Results tab is where the operator looks to decide whether
    any of this is working — so it is driven here rather than approximated.

    The wording assertions are not cosmetic. On an install where nothing has
    been promoted, the block has to say that plainly; a panel that merely
    showed "SHADOW" without saying no candidate has qualified reads as though
    the layer is doing something for you, which is the one impression this
    patch must never give.
    """
    dash._fill_consistency()
    text = dash.consistency_note.text()

    assert "CONSISTENCY ENGINE" in text
    assert "Thesis health" in text
    assert "Risk guard:" in text and "NORMAL" in text
    assert "SHADOW" in text
    assert "No candidate has met the promotion bar" in text


def test_the_consistency_block_reports_the_open_book_not_history(dash):
    """The thesis census answers "what am I holding", so it must move when the
    book moves — and a closed position must not appear in it."""
    from pqb.journal import Journal

    # Through the Journal rather than raw sqlite3: it creates the state
    # directory and applies the schema, which is what the running bot does.
    journal = Journal(dash.cfg.journal_path)
    try:
        journal._conn.execute(
            "INSERT INTO lifecycles(token_id, status, thesis_state) "
            "VALUES('a','OPEN','WEAKENING')")
        journal._conn.execute(
            "INSERT INTO lifecycles(token_id, status, thesis_state) "
            "VALUES('b','CLOSED','INVALIDATED')")
        journal._conn.commit()
    finally:
        journal.close()

    dash.reader._consistency_cache = None
    dash._fill_consistency()
    text = dash.consistency_note.text()
    assert "weakening 1" in text
    assert "invalidated 0" in text          # closed, so not in the book
    assert "WATCHING" in text               # ...and the guard says so
