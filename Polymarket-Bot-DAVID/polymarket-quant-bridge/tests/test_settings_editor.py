"""Editing the config from the dashboard.

The config is heavily commented and a person may also hand-edit it, so an edit
must change one value and touch nothing else.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pqb.config import load
from pqb.gui import settings

SRC = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"


@pytest.fixture
def cfg_file(tmp_path) -> Path:
    target = tmp_path / "config.yaml"
    shutil.copy(SRC, target)
    return target


def test_reads_every_exposed_setting(cfg_file):
    values = settings.read(cfg_file)
    missing = [s.path for s in settings.EDITABLE if s.path not in values]
    assert not missing, f"could not find: {missing}"


def test_edited_values_stay_numbers(cfg_file):
    """The bug this guards: a rewrite that eats the space before a trailing
    comment makes YAML read '0.25# per position…' as a STRING. The config still
    loads, and the engine then does arithmetic on text."""
    settings.write(cfg_file, {"engine.portfolio.max_position_fraction": 0.20,
                              "engine.portfolio.fee_per_trade_usdc": 0.03})
    cfg = load(cfg_file)
    assert isinstance(cfg.engine.portfolio.max_position_fraction, float)
    assert isinstance(cfg.engine.portfolio.fee_per_trade_usdc, float)
    assert cfg.engine.portfolio.max_position_fraction == 0.20


def test_comments_survive(cfg_file):
    before = [l for l in cfg_file.read_text(encoding="utf-8").splitlines()
              if l.strip().startswith("#")]
    settings.write(cfg_file, {"engine.portfolio.max_position_fraction": 0.20})
    after = [l for l in cfg_file.read_text(encoding="utf-8").splitlines()
             if l.strip().startswith("#")]
    assert after == before


def test_only_the_named_line_changes(cfg_file):
    before = cfg_file.read_text(encoding="utf-8").splitlines()
    settings.write(cfg_file, {"engine.portfolio.max_position_fraction": 0.20})
    after = cfg_file.read_text(encoding="utf-8").splitlines()
    assert len(before) == len(after)
    differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(differing) == 1


def test_nested_keys_do_not_collide(cfg_file):
    """A nested key must match only its own line, not a like-named neighbour."""
    settings.write(cfg_file, {"engine.portfolio.min_order_usdc": 3.0})
    cfg = load(cfg_file)
    assert cfg.engine.portfolio.min_order_usdc == 3.0
    # Neighbouring settings untouched.
    assert cfg.engine.portfolio.max_position_fraction == 0.25


def test_writing_the_same_value_changes_nothing(cfg_file):
    values = settings.read(cfg_file)
    original = cfg_file.read_text(encoding="utf-8")
    assert settings.write(cfg_file, values) == []
    assert cfg_file.read_text(encoding="utf-8") == original


def test_ints_stay_ints(cfg_file):
    settings.write(cfg_file, {"markets.filters.max_markets": 30,
                              "engine.cycle_seconds": 10})
    cfg = load(cfg_file)
    assert cfg.markets.filters.max_markets == 30
    assert cfg.engine.cycle_seconds == 10
    assert isinstance(cfg.markets.filters.max_markets, int)


def test_the_file_always_stays_loadable(cfg_file):
    for setting in settings.EDITABLE:
        settings.write(cfg_file, {setting.path: setting.low})
        load(cfg_file)          # must not raise for any single setting


def test_the_shipped_config_actually_validates():
    """The regression that cost a night of testing: config.yaml shipped with
    cycle_seconds: 2 while validate() still demanded >= 5, so the bot exited
    instantly on every Start with its stderr thrown away. The shipped file and
    the validator must never disagree again."""
    for name in ("config.example.yaml", "config.yaml"):
        path = SRC.parent / name
        if not path.exists():
            continue
        cfg = load(path)
        assert cfg.validate() == [], f"{name} does not pass its own validation"
