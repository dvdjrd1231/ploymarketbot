"""Real-process lifecycle: the actual bot as a child process, start to stop.

Named test_aa_* deliberately: these spawn real python children whose boot is
network-bound, and once the GUI-flow tests have loaded Qt into the runner
process, child startup slows enough under contention to breach even generous
waits. Running first keeps them deterministic; the ordering is load-bearing.

Not a simulation of the flow — the flow. A real `pqb.cli run` child against a
throwaway project, stopped through the same STOP-file mechanism the buttons
use, including the detached case (a second window that never spawned it).
Slow by unit-test standards (seconds, one network-less config) and worth
every one of them: this is the exact path that kept failing in operators'
hands while the unit suite was green.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC_CONFIG = REPO / "config" / "config.example.yaml"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A throwaway project dir with a valid config, offline-friendly."""
    (tmp_path / "config").mkdir()
    cfg = tmp_path / "config" / "config.yaml"
    text = SRC_CONFIG.read_text(encoding="utf-8")
    # Keep the child cheap and network-quiet where possible.
    text = text.replace("enabled: true\n  db: \"state/intel.sqlite3\"",
                        "enabled: false\n  db: \"state/intel.sqlite3\"")
    cfg.write_text(text, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    return {"root": tmp_path, "config": cfg, "env": env}


def _spawn(project):
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(
        [sys.executable, "-m", "pqb.cli", "--config", str(project["config"]),
         "run"],
        cwd=str(project["root"]), env=project["env"], creationflags=flags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_for(predicate, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return False


def test_start_heartbeat_stop_via_stop_file(project):
    """The full operator flow with a REAL bot process."""
    proc = _spawn(project)
    try:
        journal = project["root"] / "state" / "journal.sqlite3"
        assert _wait_for(journal.exists, 120), "bot never created its journal"
        assert proc.poll() is None, "bot died at startup"

        # The detached-window case: a BotProcess that did NOT spawn this bot
        # must still be able to stop it — this is the button that was dead.
        from pqb.gui.app import BotProcess
        other_window = BotProcess(project["config"], project["root"])
        assert not other_window.alive
        other_window.request_stop()
        stop_file = project["root"] / "state" / "STOP"
        assert stop_file.exists()

        assert _wait_for(lambda: proc.poll() is not None, 120), \
            "bot ignored the STOP file"
        assert proc.returncode == 0, "bot did not exit cleanly"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_reset_actually_works_once_the_bot_is_stopped(project):
    """Start real bot -> stop it -> perform_reset must succeed (no lock)."""
    proc = _spawn(project)
    try:
        journal = project["root"] / "state" / "journal.sqlite3"
        assert _wait_for(journal.exists, 120)
        (project["root"] / "state" / "STOP").write_text("stop",
                                                        encoding="utf-8")
        assert _wait_for(lambda: proc.poll() is not None, 120)

        from pqb.config import load
        from pqb.gui.app import perform_reset
        removed = perform_reset(load(project["config"]))
        assert "journal.sqlite3" in removed
        assert not journal.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x00100000, 0, pid)
    if not handle:
        return False
    alive = ctypes.windll.kernel32.WaitForSingleObject(handle, 0) != 0
    ctypes.windll.kernel32.CloseHandle(handle)
    return alive


@pytest.mark.skipif(os.name != "nt", reason="Windows watchdog")
def test_watchdog_reaps_a_stuck_bot_after_the_window_dies(tmp_path):
    """The 'python still in Task Manager' report: a bot that ignores its stop
    must be reaped by the detached watchdog even with no window left."""
    from pqb.gui.app import BotProcess

    stubborn = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        window = BotProcess(tmp_path / "c.yaml", tmp_path)
        window._spawn_watchdog(stubborn.pid, wait_seconds=2)
        # The window object dying changes nothing - the watchdog is detached.
        del window
        assert _wait_for(lambda: not _pid_alive(stubborn.pid), 25), \
            "watchdog failed to reap the stuck process"
    finally:
        if stubborn.poll() is None:
            stubborn.kill()


@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill tree")
def test_force_kill_takes_the_whole_process_tree(tmp_path):
    """A kill that only reaches the parent is how orphans are made."""
    from pqb.gui.app import BotProcess

    parent = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess,sys,time\n"
         "child=subprocess.Popen([sys.executable,'-c','import time;"
         "time.sleep(300)'])\n"
         "print(child.pid,flush=True)\n"
         "time.sleep(300)"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        grandchild_pid = int(parent.stdout.readline().strip())
        assert _pid_alive(grandchild_pid)

        window = BotProcess(tmp_path / "c.yaml", tmp_path)
        window._force_kill(parent.pid)
        assert _wait_for(lambda: not _pid_alive(parent.pid), 20)
        assert _wait_for(lambda: not _pid_alive(grandchild_pid), 20), \
            "grandchild survived the tree kill"
    finally:
        for pid in (parent.pid,):
            if parent.poll() is None:
                parent.kill()
        subprocess.run(["taskkill", "/PID", str(parent.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
