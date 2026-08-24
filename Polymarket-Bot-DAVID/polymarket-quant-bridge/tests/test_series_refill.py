"""The dataset self-heals on an UPGRADED install, not just a fresh one.

The fresh-install trigger counts resolutions, and a store full of LEGACY
settled-tail tapes passes it — while the uncertainty band drops nearly every
series it holds. Field report: many hours of running, no discovery output at
all, because research kept studying data the band correctly refused and
nothing ever re-collected. These pin the refill trigger that closes the loop.
"""

from __future__ import annotations

import time
import types

from pqb.config import Config
from pqb.logs import Log
from pqb.runner import Runner


def _runner(tmp_path) -> Runner:
    cfg = Config()
    cfg.root = tmp_path
    r = Runner(cfg, Log())
    r.intel_store = types.SimpleNamespace()   # present, as after start()
    return r


def test_data_starved_pass_triggers_recollection(tmp_path):
    r = _runner(tmp_path)
    assert r._needs_series_refill(exported=1) is True


def test_enough_series_means_no_refill(tmp_path):
    r = _runner(tmp_path)
    floor = r.config.research.auto_backfill_min_series
    assert r._needs_series_refill(exported=floor) is False


def test_cooldown_prevents_hammering_the_api(tmp_path):
    """One sweep per cooldown window: a market with genuinely little to offer
    must not cause a re-collection attempt every hourly pass."""
    r = _runner(tmp_path)
    r._last_backfill = time.time()            # a sweep just ran
    assert r._needs_series_refill(exported=0) is False


def test_no_refill_while_one_is_already_running(tmp_path):
    r = _runner(tmp_path)
    r._backfilling = True
    assert r._needs_series_refill(exported=0) is False


def test_disabled_auto_backfill_is_respected(tmp_path):
    r = _runner(tmp_path)
    r.config.research.auto_backfill = False
    assert r._needs_series_refill(exported=0) is False
