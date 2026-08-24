"""State lives OUTSIDE the install folder, so an update can never lose it.

The operator's requirement: the strategy library (and all other evidence)
must survive every update, including extracting the zip to a brand-new
folder. The shipped config points every state path at an external data
folder; these pin the one-time adoption move, its idempotence, and the
fallback that keeps using the old location rather than silently starting
an empty library next to a full one.
"""

from __future__ import annotations

import pqb.config as config_mod
from pqb.config import load


def _write_config(root, data_base: str):
    cfg_dir = root / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(f"""
mode:
  kill_switch_file: "{data_base}/state/KILL"
intel:
  db: "{data_base}/state/intel.sqlite3"
storage:
  data_dir: "{data_base}/state"
  journal_db: "{data_base}/state/journal.sqlite3"
logging:
  file: "{data_base}/state/pqb.log"
""", encoding="utf-8")
    return cfg_dir / "config.yaml"


def _plant_legacy(root):
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "library.sqlite3").write_bytes(b"precious evidence")
    (state / "intel.sqlite3").write_bytes(b"captured history")
    return state


def test_existing_state_is_adopted_by_moving(tmp_path):
    install = tmp_path / "Polymarket-Bot-DAVID" / "polymarket-quant-bridge"
    path = _write_config(install, "../../Polymarket-Bot-DATA")
    _plant_legacy(install)
    cfg = load(path)
    external = tmp_path / "Polymarket-Bot-DATA" / "state"
    assert cfg.data_dir.resolve() == external.resolve()
    assert (external / "library.sqlite3").read_bytes() == b"precious evidence"
    assert (external / "MIGRATED-FROM.txt").exists()
    # MOVED, not copied: no stale twin left inside the install folder.
    assert not (install / "state" / "library.sqlite3").exists()


def test_adoption_is_one_time_never_a_clobber(tmp_path):
    install = tmp_path / "Polymarket-Bot-DAVID" / "polymarket-quant-bridge"
    path = _write_config(install, "../../Polymarket-Bot-DATA")
    external = tmp_path / "Polymarket-Bot-DATA" / "state"
    external.mkdir(parents=True)
    (external / "library.sqlite3").write_bytes(b"the real library")
    # A NEW install folder appears (fresh extract) with its own empty-ish
    # state - it must never overwrite the external library.
    legacy = _plant_legacy(install)
    (legacy / "library.sqlite3").write_bytes(b"fresh empty library")
    cfg = load(path)
    assert (external / "library.sqlite3").read_bytes() == b"the real library"
    assert cfg.data_dir.resolve() == external.resolve()


def test_fresh_install_just_uses_the_external_location(tmp_path):
    install = tmp_path / "Polymarket-Bot-DAVID" / "polymarket-quant-bridge"
    path = _write_config(install, "../../Polymarket-Bot-DATA")
    cfg = load(path)          # no legacy state anywhere
    assert "Polymarket-Bot-DATA" in str(cfg.data_dir)


def test_dev_default_in_install_state_is_untouched(tmp_path):
    install = tmp_path / "repo"
    cfg_dir = install / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text("storage:\n  data_dir: \"state\"\n",
                                         encoding="utf-8")
    state = _plant_legacy(install)
    cfg = load(cfg_dir / "config.yaml")
    assert cfg.data_dir.resolve() == state.resolve()
    assert (state / "library.sqlite3").exists()


def test_failed_move_falls_back_to_legacy_not_empty(tmp_path, monkeypatch):
    """Losing the move must not lose the library: the run continues on the
    in-install state instead of silently starting an empty external one."""
    install = tmp_path / "Polymarket-Bot-DAVID" / "polymarket-quant-bridge"
    path = _write_config(install, "../../Polymarket-Bot-DATA")
    _plant_legacy(install)

    def _refuse(src, dst):
        raise OSError("locked")

    monkeypatch.setattr(config_mod.shutil, "move", _refuse)
    cfg = load(path)
    assert cfg.data_dir.resolve() == (install / "state").resolve()
    assert (install / "state" / "library.sqlite3").exists()
    assert cfg.intel_path.resolve() == \
        (install / "state" / "intel.sqlite3").resolve()
