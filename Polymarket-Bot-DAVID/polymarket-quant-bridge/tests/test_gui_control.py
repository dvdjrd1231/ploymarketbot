"""Starting and stopping the bot from the dashboard.

These guard a bug that looked, to the client, exactly like a dead button: the
stop blocked the GUI thread for up to 45s (so nothing repainted) AND the signal
it sent could never arrive (the child has no console), so the bot kept running.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from pqb.gui.app import STOP_GRACE_SECONDS, STOP_KILL_SECONDS, BotProcess


class FakeProc:
    """A stand-in child process.

    ``ignore_terminate`` models the only situation the hard-kill path exists
    for: a process that will not die politely. Without it the fake dies on
    terminate, `alive` goes False, and poll correctly stops escalating — which
    would make the kill branch untestable rather than broken.
    """

    def __init__(self, exits_after: float = 0.0, ignore_terminate: bool = False):
        self._born = time.time()
        self._after = exits_after
        self._ignore_terminate = ignore_terminate
        self.terminated = self.killed = False
        self.signalled = []

    def poll(self):
        if self.killed or (self.terminated and not self._ignore_terminate):
            return -1
        return 0 if time.time() - self._born >= self._after else None

    def send_signal(self, sig):
        self.signalled.append(sig)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def bot(tmp_path, exits_after=0.0, ignore_terminate=False) -> BotProcess:
    b = BotProcess(tmp_path / "config.yaml", tmp_path,
                   stop_file=tmp_path / "state" / "STOP")
    b.proc = FakeProc(exits_after, ignore_terminate)
    b._started_at = 0.0                       # past the "starting" window
    return b


def test_states_move_stopped_running_stopping_stopped(tmp_path):
    b = bot(tmp_path, exits_after=999)
    assert b.state == "running"
    b.request_stop()
    assert b.state == "stopping"              # immediately, not on the next poll
    b.proc.terminated = True                  # the child goes away
    b.poll()
    assert b.state == "stopped"


def test_request_stop_does_not_block(tmp_path):
    """It used to wait on the child for up to 45s on the GUI thread."""
    b = bot(tmp_path, exits_after=999)
    started = time.perf_counter()
    b.request_stop()
    assert (time.perf_counter() - started) < 0.5


def test_stop_writes_the_file_because_a_signal_cannot_arrive(tmp_path):
    """The child is launched with no console, so CTRL_BREAK never lands.

    The file is the mechanism that actually works; without it the bot ran on
    after every press of Stop.
    """
    b = bot(tmp_path, exits_after=999)
    b.request_stop()
    assert b.stop_file.exists()
    assert b.stop_file.read_text(encoding="utf-8").strip() == "stop"


def test_pressing_stop_twice_is_harmless(tmp_path):
    b = bot(tmp_path, exits_after=999)
    b.request_stop()
    first = b._stop_at
    b.request_stop()
    assert b._stop_at == first                # not restarted, not re-escalated


def test_a_stop_that_hangs_is_escalated_then_killed(tmp_path):
    b = bot(tmp_path, exits_after=999, ignore_terminate=True)
    b.request_stop()
    b._stop_at = time.time() - (STOP_GRACE_SECONDS + 1)
    b.poll()
    assert b.proc.terminated, "should terminate once the grace period passes"
    b._stop_at = time.time() - (STOP_KILL_SECONDS + 1)
    b.poll()
    assert b.proc.killed, "should hard-kill if terminate did not take"


def test_starting_shows_before_running(tmp_path):
    b = BotProcess(tmp_path / "c.yaml", tmp_path,
                   stop_file=tmp_path / "STOP")
    b.proc = FakeProc(exits_after=999)
    b._started_at = time.time()
    assert b.state == "starting"
    b._started_at = time.time() - 10
    assert b.state == "running"


def test_a_stale_stop_file_cannot_kill_the_next_run():
    """Stale (pre-boot) stop files are cleared on entry — but ONLY pre-boot
    ones: a stop pressed during our own boot must survive to be honoured.
    The honouring side is proven with a real child process in
    test_bot_lifecycle.py; this pins the stale/fresh distinction in code."""
    import inspect
    from pqb.runner import Runner
    src = inspect.getsource(Runner.run_forever)
    assert "st_mtime < self._boot_wall" in src     # stale test is time-based
    assert "unlink" in src
    honour = inspect.getsource(Runner._shutdown_requested)
    assert "unlink()" in honour


def test_the_sleep_wakes_on_a_stop_request():
    """Most of a cycle is spent asleep; without this a stop looks like a hang."""
    import inspect
    from pqb.runner import Runner
    src = inspect.getsource(Runner._sleep)
    assert "_shutdown_requested" in src
    assert "min(1.0, remaining)" in src


# --- the reset button's engine ------------------------------------------------

def _cfg_with_state(tmp_path):
    from pqb.config import Config
    cfg = Config()
    cfg.root = tmp_path
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_reset_wipes_results_but_keeps_knowledge(tmp_path):
    """Journal + control files go; the intel store and strategies stay."""
    from pqb.gui.app import perform_reset

    cfg = _cfg_with_state(tmp_path)
    cfg.journal_path.write_text("db", encoding="utf-8")
    cfg.kill_switch_path.write_text("halt", encoding="utf-8")
    cfg.halt_path.write_text("x", encoding="utf-8")
    cfg.intel_path.write_text("knowledge", encoding="utf-8")
    (cfg.data_dir / "strategies.json").write_text("{}", encoding="utf-8")

    removed = perform_reset(cfg)

    assert not cfg.journal_path.exists()
    assert not cfg.kill_switch_path.exists()
    assert not cfg.halt_path.exists()
    assert cfg.intel_path.exists()                      # knowledge kept
    assert (cfg.data_dir / "strategies.json").exists()  # discoveries kept
    assert "journal.sqlite3" in removed


def test_reset_refuses_a_live_journal(tmp_path):
    from pqb.gui.app import perform_reset

    cfg = _cfg_with_state(tmp_path)
    cfg.mode.dry_run = False
    cfg.mode.allow_live = True
    cfg.journal_path.write_text("real money audit trail", encoding="utf-8")
    try:
        perform_reset(cfg)
        assert False, "must refuse to reset a live journal"
    except RuntimeError:
        pass
    assert cfg.journal_path.exists()


def test_reset_on_a_fresh_install_is_a_no_op(tmp_path):
    from pqb.gui.app import perform_reset

    assert perform_reset(_cfg_with_state(tmp_path)) == []


def test_stop_reaches_a_bot_this_window_did_not_spawn(tmp_path):
    """The operator's 'these buttons don't work': a bot left running by an
    earlier window watches the STOP file; the button must write it even when
    self.proc is None."""
    from pqb.gui.app import BotProcess

    bot = BotProcess(tmp_path / "config.yaml", tmp_path)
    assert not bot.alive                       # nothing spawned by this window
    bot.request_stop()
    assert bot.stop_file.exists()
    assert bot.stop_file.read_text(encoding="utf-8").strip() == "stop"


def test_auto_backfill_triggers_only_on_a_thin_store(tmp_path):
    """Fresh install self-provisions; a deep store does not re-pull."""
    from pqb.config import Config
    from pqb.runner import Runner
    from pqb.logs import Log

    cfg = Config()
    cfg.root = tmp_path
    runner = Runner(cfg, Log())

    class _Store:
        def __init__(self, n): self.n = n
        def stats(self): return {"resolutions": self.n}

    runner.intel_store = _Store(5)
    assert runner._needs_backfill()
    runner.intel_store = _Store(2_000)
    assert not runner._needs_backfill()
    cfg.research.auto_backfill = False
    runner.intel_store = _Store(5)
    assert not runner._needs_backfill()
