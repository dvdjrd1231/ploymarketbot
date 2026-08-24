"""
The desktop dashboard.

Written for someone who wants to know *what the bot is doing and whether it is
making money* — not for someone reading log lines. Every panel answers a
question in the words a person would actually ask.

**The bot runs as a separate process.** This window starts it, stops it, and
reads its databases; it never runs the trading loop itself. So closing the
window does not close a position, and a fault in the interface cannot take the
bot down mid-cycle. The two stop controls write the same files the command line
uses, which means they work even if this window is unresponsive.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .qt import (
    QAction, QApplication, QCheckBox, QColor, QDoubleSpinBox, QFont, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QTimer, QVBoxLayout, QWidget,
    Qt, VERTICAL, ALIGN_TOP,
)
from .reader import Reader

REFRESH_MS = 4000

GREEN, RED, AMBER, MUTED = "#1f9d55", "#c53030", "#b7791f", "#6b7280"

# The explainer panels' style, switched as one piece by apply_theme() so a
# dark run cannot end up half-light: light OS chrome with hardcoded light
# panels looked fine, dark chrome with those same panels looked broken.
PANEL_STYLE = ("background:#f8fafc; color:#1a2733; border:1px solid #e2e8f0; "
               "padding:12px;")
_DARK_PANEL_STYLE = ("background:#1e2530; color:#d7dde6; "
                     "border:1px solid #333d4d; padding:12px;")

# The drag divider and its collapse link, flipped with the theme for the same
# reason as the panels: a light-grey handle on dark chrome reads as damage.
SPLIT_HANDLE_STYLE = ("QSplitter::handle{background:#e2e8f0; margin:2px 0;}"
                      "QSplitter::handle:hover{background:#94a3b8;}")
_DARK_SPLIT_HANDLE_STYLE = (
    "QSplitter::handle{background:#333d4d; margin:2px 0;}"
    "QSplitter::handle:hover{background:#5a6b80;}")
COLLAPSE_LINK_STYLE = (
    "QPushButton{border:none; padding:2px 4px; color:#6b7280;}"
    "QPushButton:hover{color:#1a2733; text-decoration:underline;}")
_DARK_COLLAPSE_LINK_STYLE = (
    "QPushButton{border:none; padding:2px 4px; color:#8b95a3;}"
    "QPushButton:hover{color:#d7dde6; text-decoration:underline;}")


def apply_theme(app, dark: bool) -> None:
    """Explicit theme, not an OS guess. Dark = Fusion + a dark palette, and
    the panel style flips with it; light leaves everything as designed."""
    global PANEL_STYLE, SPLIT_HANDLE_STYLE, COLLAPSE_LINK_STYLE
    if not dark:
        return
    from .qt import QColor
    from PyQt6.QtGui import QPalette
    app.setStyle("Fusion")
    palette = QPalette()
    base = QColor("#151a21")
    panel = QColor("#1e2530")
    text = QColor("#d7dde6")
    palette.setColor(QPalette.ColorRole.Window, base)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, panel)
    palette.setColor(QPalette.ColorRole.AlternateBase, base)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, panel)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.ToolTipBase, panel)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(MUTED))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#2f6fdd"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Link, QColor("#6ea8ff"))
    app.setPalette(palette)
    PANEL_STYLE = _DARK_PANEL_STYLE
    SPLIT_HANDLE_STYLE = _DARK_SPLIT_HANDLE_STYLE
    COLLAPSE_LINK_STYLE = _DARK_COLLAPSE_LINK_STYLE


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _attack_cell(strategy: dict) -> str:
    """One cell summarising what deliberate attack found. -> e.g. "BROKEN 0.37
    (60%) concentration, drawdown_stress"

    Three numbers travel together here for a reason. The verdict alone reads
    as a grade; the robustness alone hides how much of the battery could
    actually run; and coverage alone says nothing about the outcome. A
    SURVIVED at 30% coverage is a candidate that dodged most of the questions,
    and showing it as plain "SURVIVED" would manufacture exactly the
    confidence the battery exists to withhold.
    """
    verdict = str(strategy.get("adversarialVerdict") or "")
    if not verdict or verdict == "NOT_ATTACKED":
        # Not a gap in the data: the battery refuses candidates whose record
        # is too thin to attack, and saying so beats an empty cell.
        return "not attacked"
    coverage = float(strategy.get("adversarialCoverage") or 0.0)
    cell = (f"{verdict} {float(strategy.get('robustness') or 0.0):.2f} "
            f"({coverage * 100:.0f}%)")
    failed = [str(f) for f in (strategy.get("adversarialFailed") or [])]
    return cell + (" " + ", ".join(failed[:2]) if failed else "")


def _hold_label(seconds) -> str:
    """Holding periods run from seconds to weeks; one unit cannot show
    that, and "0.0h" for a two-minute hold reads as missing data."""
    if not seconds:
        return ""
    seconds = float(seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60.0:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600.0:.1f}h"
    return f"{seconds / 86400.0:.1f}d"


class Card(QGroupBox):
    """One headline number with a caption underneath."""

    def __init__(self, title: str, caption: str = ""):
        super().__init__(title)
        layout = QVBoxLayout(self)
        self.value = QLabel("—")
        font = self.value.font()
        font.setPointSize(20)
        font.setBold(True)
        self.value.setFont(font)
        self.caption = QLabel(caption)
        self.caption.setStyleSheet(f"color: {MUTED};")
        self.caption.setWordWrap(True)
        layout.addWidget(self.value)
        layout.addWidget(self.caption)
        layout.addStretch()

    def set(self, value: str, caption: str = "", colour: str = "") -> None:
        self.value.setText(value)
        self.value.setStyleSheet(f"color: {colour};" if colour else "")
        if caption:
            self.caption.setText(caption)


class Table(QTableWidget):
    def __init__(self, headers: list[str]):
        super().__init__(0, len(headers))
        self.setHorizontalHeaderLabels(headers)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)

    def fill(self, rows: list[list], colours: dict[int, str] | None = None):
        self.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                item = QTableWidgetItem(str(cell))
                if colours and r in colours:
                    item.setForeground(QColor(colours[r]))
                self.setItem(r, c, item)


# A graceful stop asks the bot to finish the cycle it is in. That is usually
# instant (it is asleep between cycles) but can take a few seconds mid-cycle.
# Past this, something is wrong and it is killed instead.
STOP_GRACE_SECONDS = 30.0


def perform_reset(cfg) -> list[str]:
    """Wipe the SIMULATED account so the next start begins fresh at $100.

    The operator's testing loop: run, watch, wipe, run again. This deletes the
    journal (paper book, positions, closed trades, decisions, predictions, the
    doubling progression) and clears the KILL/STOP/HALT files — everything that
    is *result*, nothing that is *knowledge*. The intel store (wallet history,
    rankings, research capture) and discovered strategies survive on purpose:
    resetting a test account should not cost days of collected market history.

    Refuses to touch a live-mode journal: that file is the audit trail of real
    money and is not a cache. Returns what was removed, for the status bar.
    """
    if cfg.mode.live:
        raise RuntimeError("Reset is for the simulation. This configuration is "
                           "in LIVE mode, and a live journal is an audit "
                           "record, not a cache.")
    removed: list[str] = []
    journal = cfg.journal_path
    targets = [journal,
               Path(str(journal) + "-wal"), Path(str(journal) + "-shm"),
               cfg.kill_switch_path, cfg.stop_path, cfg.halt_path,
               cfg.data_dir / "last-start.log"]
    for path in targets:
        # One short retry: a dashboard refresh query can hold the file for a
        # moment. A REAL lock (a running bot) survives the retry and errors.
        for attempt in (0, 1):
            try:
                if path.exists():
                    path.unlink()
                    removed.append(path.name)
                break
            except OSError as exc:
                if attempt == 0:
                    time.sleep(0.4)
                    continue
                raise RuntimeError(
                    f"Could not remove {path.name}: {exc}. A bot is still "
                    "running somewhere (possibly started from an earlier "
                    "window) - press 'Stop bot', wait for 'stopped', then "
                    "reset again.") from exc
    return removed
STOP_KILL_SECONDS = 40.0


class BotProcess:
    """Starts and stops `pqb.cli run` as a child process.

    **Nothing here blocks.** An earlier version waited on the child inside
    :meth:`stop`, which ran on the GUI thread — so pressing "Stop bot" froze
    the entire window for as long as the shutdown took, with no repaint, no
    button change and no explanation. It looked exactly like a button that did
    nothing.

    Instead this exposes a state that :meth:`poll` advances, and the window
    drives it from a timer. Every transition is therefore visible while it
    happens.
    """

    def __init__(self, config_path: Path, root: Path,
                 stop_file: Path | None = None,
                 err_path: Path | None = None):
        self.config_path = config_path
        self.root = root
        self.stop_file = stop_file or (root / "state" / "STOP")
        # Resolved from config by the caller: with externalized state this
        # is NOT under root, and writing it there while the reset/banner
        # read the data dir would split the record in two.
        self._err_path = err_path or (root / "state" / "last-start.log")
        self.proc: subprocess.Popen | None = None
        self._stop_at: float | None = None
        self._killed = False
        self._started_at = 0.0

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def state(self) -> str:
        """``stopped`` | ``starting`` | ``running`` | ``stopping``."""
        if self._stop_at is not None:
            return "stopping" if self.alive else "stopped"
        if not self.alive:
            return "stopped"
        # A brief "starting" so the first press gives immediate feedback rather
        # than looking idle until the first cycle lands.
        return "starting" if time.time() - self._started_at < 4.0 else "running"

    # Kept so existing callers reading `.running` still work.
    @property
    def running(self) -> bool:
        return self.alive

    @property
    def stopping_for(self) -> float:
        return 0.0 if self._stop_at is None else max(0.0, time.time() - self._stop_at)

    def start(self) -> None:
        if self.alive:
            return
        flags = 0
        if os.name == "nt":
            # A new process group is what makes a *graceful* stop possible on
            # Windows: CTRL_BREAK can be delivered to the group, and the runner
            # finishes its cycle and closes the journal cleanly. Without it the
            # only option is a hard kill mid-write.
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        # stderr goes to a file, never to DEVNULL. A child that dies at startup
        # (a config error, a missing dependency) used to vanish silently — the
        # operator pressed Start, nothing happened, and the reason was thrown
        # away. Now start_error() can read back exactly what the child said.
        try:
            self._err_path.parent.mkdir(parents=True, exist_ok=True)
            err_handle = open(self._err_path, "w", encoding="utf-8")
        except OSError:
            err_handle = subprocess.DEVNULL
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "pqb.cli", "--config",
             str(self.config_path), "run"],
            cwd=str(self.root), creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=err_handle,
        )
        if err_handle is not subprocess.DEVNULL:
            # The child inherited the handle; the parent's copy is done with.
            err_handle.close()
        self._stop_at = None
        self._killed = False
        self._started_at = time.time()

    def start_error(self) -> str:
        """What the bot said before dying on its own. Empty when healthy.

        Covers both faces of the same complaint: "I press Start and nothing
        happens" (a config error killed it in milliseconds) and "it was running
        when I went to sleep and dead in the morning" (a crash hours in). A
        stop the operator asked for is not an error and reports nothing.
        """
        if self.alive or self.proc is None:
            return ""
        if self._stop_at is not None or self._killed:
            return ""                      # the operator stopped it on purpose
        try:
            text = (self._err_path.read_text(encoding="utf-8").strip()
                    if getattr(self, "_err_path", None)
                    and self._err_path.exists() else "")
        except OSError:
            text = ""
        if not text:
            return ("it exited without a message - see state/pqb.log for its "
                    "last minutes")
        return text[-600:]

    def request_stop(self) -> None:
        """Ask the bot to shut down. Returns immediately.

        A **file** is the primary mechanism, not a signal. The child is
        launched with no console so that no command prompt appears, and a
        console control event has nothing to be delivered to in that case — it
        never arrives, and the bot runs on regardless. The runner watches for
        this file between cycles and inside its sleep, so it reacts within a
        second. The signal is still sent afterwards as a belt-and-braces on
        platforms where it does work.
        """
        if self._stop_at is not None:
            return
        # The STOP file is written even when THIS window did not spawn the bot:
        # a bot left running by an earlier window ("Leave it running") watches
        # the same file, and skipping it made the Stop button silently dead
        # against exactly those bots — "these buttons don't work", literally.
        self._stop_at = time.time()
        self._killed = False
        with contextlib.suppress(Exception):
            self.stop_file.parent.mkdir(parents=True, exist_ok=True)
            self.stop_file.write_text("stop\n", encoding="utf-8")
        if not self.alive:
            return                     # no child of ours to signal or poll
        # The watchdog survives this window. Without it, closing the window
        # right after Stop left a hung bot with no escalation — the "python
        # still in Task Manager" report, exactly.
        pid = getattr(self.proc, "pid", None)
        if isinstance(pid, int):
            self._spawn_watchdog(pid)
        with contextlib.suppress(Exception):
            if os.name == "nt":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.send_signal(signal.SIGINT)

    def _force_kill(self, pid) -> None:
        """Kill the bot AND anything under it.

        ``taskkill /T`` takes the whole tree — today the bot has no children,
        but a kill that only reaches the parent is exactly how "there is still
        a python in Task Manager" happens the day something ever does spawn.
        """
        if os.name == "nt" and isinstance(pid, int):
            with contextlib.suppress(Exception):
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW,
                               timeout=15)
                return
        with contextlib.suppress(Exception):
            if self.proc is not None:
                self.proc.kill()

    def _spawn_watchdog(self, pid: int,
                        wait_seconds: float = STOP_KILL_SECONDS + 5) -> None:
        """A detached sentinel that outlives this window.

        The escalation below runs on the window's timer — so closing the
        window right after pressing Stop used to leave a hung bot with nobody
        to escalate, and a python lingered in Task Manager. This tiny detached
        process waits for the pid to exit and tree-kills it if the grace runs
        out, whether or not the dashboard still exists.
        """
        if os.name != "nt":
            return
        script = (
            "import ctypes,subprocess,sys\n"
            "pid=int(sys.argv[1]); wait_ms=int(float(sys.argv[2])*1000)\n"
            "k=ctypes.windll.kernel32\n"
            "h=k.OpenProcess(0x00100000,0,pid)\n"          # SYNCHRONIZE
            "if h:\n"
            "    k.WaitForSingleObject(h,wait_ms)\n"
            "    alive=k.WaitForSingleObject(h,0)!=0\n"
            "    k.CloseHandle(h)\n"
            "    if alive:\n"
            "        subprocess.call(['taskkill','/PID',str(pid),'/T','/F'],\n"
            "                        stdout=subprocess.DEVNULL,\n"
            "                        stderr=subprocess.DEVNULL)\n"
        )
        with contextlib.suppress(Exception):
            subprocess.Popen(
                [sys.executable, "-c", script, str(pid), str(wait_seconds)],
                creationflags=(subprocess.CREATE_NO_WINDOW
                               | subprocess.DETACHED_PROCESS),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL)

    def poll(self) -> None:
        """Advance a pending stop. Called from the window's timer."""
        if self._stop_at is None:
            return
        if not self.alive:
            self._stop_at = None
            return
        waited = time.time() - self._stop_at
        # Escalate rather than wait forever. A hard stop costs at most the
        # cycle in progress — every decision already taken is committed.
        if waited > STOP_KILL_SECONDS:
            self._force_kill(getattr(self.proc, "pid", None))
        elif waited > STOP_GRACE_SECONDS and not self._killed:
            self._killed = True
            with contextlib.suppress(Exception):
                self.proc.terminate()


class Dashboard(QMainWindow):
    def __init__(self, config, config_path: Path):
        super().__init__()
        self.cfg = config
        self.config_path = Path(config_path)
        self.reader = Reader(config)
        self.bot = BotProcess(config_path, Path(config.root),
                              stop_file=config.stop_path,
                              err_path=config.data_dir / "last-start.log")
        self._was_stopping = False
        self.setWindowTitle("Polymarket Quant Bridge")
        self.resize(1180, 760)

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.addLayout(self._controls())

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        self._build_overview()
        self._build_table_tab("Wallets", [
            "#", "Wallet", "Score", "Confidence", "Trades scored", "Win %",
            "Avg return", "Markets", "Plays both sides",
            # Behavioural research overlay (written by the research pass):
            # what the wallet DOES, kept separate from what it scored.
            "Settled mkts", "Hedge/reversal", "2-sided exp.",
            "1-sided exp.", "Incremental", "Median hold", "Entry bias",
            "Sample quality", "Research priority", "Top group"])
        self._build_table_tab("Unusual activity", [
            "When", "What", "Who / where", "How unusual", "Strength"])
        # Open positions carry the risk columns the money-management layer
        # needs a person to see: what is deployed, how much of the account it
        # is, whether it is correlated with something else already open, and
        # how far it has been against us so far.
        self._build_table_tab("Open positions", [
            "Outcome", "Market", "Shares", "Bought at", "Cost", "Now",
            "Unrealised", "% of equity", "Correlated with", "Held for",
            "Worst so far", "Best so far"])
        self._build_table_tab("Closed trades", [
            "Outcome", "Bought", "Sold", "Result", "Return", "Why it exited",
            "Held for", "% of equity", "Fees", "If held 30m longer"])
        self._build_results()
        self._build_discovery()
        self._build_activity()
        self._build_settings()

        self.statusBar().showMessage("Starting up…")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._timed_refresh)
        self.timer.start(REFRESH_MS)

        # A second, fast timer purely for the run state. Reading the data is
        # comparatively expensive and runs on the slow timer; whether the bot
        # is starting or stopping is a single non-blocking poll, and it is the
        # one thing that must react at the speed of a button press.
        self.state_timer = QTimer(self)
        self.state_timer.timeout.connect(self.sync_run_state)
        self.state_timer.start(400)
        self.refresh()

    # -- top bar -------------------------------------------------------------

    def _controls(self):
        bar = QHBoxLayout()
        self.mode_label = QLabel()
        font = self.mode_label.font()
        font.setBold(True)
        self.mode_label.setFont(font)
        bar.addWidget(self.mode_label)

        # Sits next to the mode so the answer to "did my click do anything?"
        # is beside the buttons rather than in a status bar at the bottom.
        self.run_state = QLabel()
        self.run_state.setFont(font)
        bar.addSpacing(14)
        bar.addWidget(self.run_state)
        bar.addStretch()

        self.start_btn = QPushButton("Start bot")
        self.start_btn.clicked.connect(self.on_start)
        self.stop_btn = QPushButton("Stop bot")
        self.stop_btn.clicked.connect(self.on_stop)
        self.panic_btn = QPushButton("STOP TRADING")
        self.panic_btn.setStyleSheet(
            f"background:{RED}; color:white; font-weight:bold; padding:6px 14px;")
        self.panic_btn.clicked.connect(self.on_panic)
        # The way back. Doing exactly what 9-RESUME.bat does (clear state/KILL),
        # as a button beside the switch that armed it — because "delete a file"
        # is not an instruction a dashboard should hand its operator. Hidden
        # unless the kill switch is actually armed.
        self.resume_btn = QPushButton("Resume trading")
        self.resume_btn.setStyleSheet(
            f"background:{GREEN}; color:white; font-weight:bold; padding:6px 14px;")
        self.resume_btn.clicked.connect(self.on_resume)
        self.resume_btn.setVisible(False)
        # Start-over for testing: wipes the simulated account back to $100.
        # Distinct from Resume on purpose — clearing the stop and clearing the
        # RESULTS are different acts, and conflating them cost the operator a
        # confused morning ("I deleted the kill file and it still didn't reset").
        self.reset_btn = QPushButton("Reset account")
        self.reset_btn.setStyleSheet(
            f"background:{AMBER}; color:white; font-weight:bold; padding:6px 14px;")
        self.reset_btn.clicked.connect(self.on_reset)
        for b in (self.start_btn, self.stop_btn, self.resume_btn,
                  self.reset_btn, self.panic_btn):
            bar.addWidget(b)
        return bar

    # -- tabs ----------------------------------------------------------------

    def _build_overview(self):
        page = QWidget()
        grid = QGridLayout(page)
        self.cards = {}
        spec = [
            ("value", "Account value", "what it is worth right now"),
            ("profit", "Profit / loss", "against what you started with"),
            ("positions", "Open positions", "trades running now"),
            ("closed", "Completed trades", "and how many were winners"),
            ("wallets", "Traders watched", "found automatically"),
            ("ranked", "Traders ranked", "enough evidence to score"),
            ("unusual", "Unusual events", "spotted so far"),
            ("size", "Trade size now", "current step of your progression"),
        ]
        for i, (key, title, caption) in enumerate(spec):
            card = Card(title, caption)
            self.cards[key] = card
            grid.addWidget(card, i // 4, i % 4)

        self.explain = QLabel()
        self.explain.setWordWrap(True)
        self.explain.setStyleSheet(
            PANEL_STYLE)
        grid.addWidget(self.explain, 2, 0, 1, 4)

        # Health, then three live previews. Everything the other tabs hold, at a
        # glance — so "is it working, and what is it doing" is answerable
        # without clicking anything.
        health = QGroupBox("System")
        self.health_grid = QGridLayout(health)
        grid.addWidget(health, 3, 0, 1, 2)

        self.preview_wallets = self._preview("Top traders right now")
        self.preview_anoms = self._preview("Latest unusual activity")
        grid.addWidget(self.preview_wallets, 3, 2, 1, 1)
        grid.addWidget(self.preview_anoms, 3, 3, 1, 1)

        self.preview_acts = self._preview("Latest decisions")
        grid.addWidget(self.preview_acts, 4, 0, 1, 4)
        grid.setRowStretch(4, 1)
        self.tabs.addTab(page, "Overview")

    def _preview(self, title: str) -> QGroupBox:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        body = QLabel("—")
        # No hardcoded text colour: inherit the theme's, so it is dark on a
        # light OS theme and light on a dark one. A fixed near-black here was
        # invisible on the dark theme (dark text on a dark background).
        body.setAlignment(Qt.AlignmentFlag.AlignTop)
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(body)
        layout.addStretch()
        box.body = body
        return box

    def _build_results(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.results_note = QLabel()
        self.results_note.setWordWrap(True)
        self.results_note.setStyleSheet(
            PANEL_STYLE)
        layout.addWidget(self.results_note)
        self.results_table = Table(
            ["Grouped by", "Which", "Trades", "Won", "Average return",
             "Total profit"])
        layout.addWidget(self.results_table)

        # MONEY MANAGEMENT DIAGNOSTICS (§23). Deliberately its own block under
        # the performance table rather than more columns in it: the question
        # "which kinds of trade make money" and the question "why is the
        # account down" have different answers and are answered from different
        # evidence.
        self.mm_note = QLabel()
        self.mm_note.setWordWrap(True)
        self.mm_note.setStyleSheet(PANEL_STYLE)
        layout.addWidget(self.mm_note)
        self.mm_table = Table(
            ["What", "Which", "Trades", "Net", "Share of loss",
             "Share of profit", "Median hold", "Reading"])
        layout.addWidget(self.mm_table)

        # CONSISTENCY ENGINE (§26). One compact block, deliberately: the whole
        # study is a page of numbers and putting it here would bury the
        # performance table above it. What a person needs at a glance is the
        # shape of the return distribution, the health of what is currently
        # open, and whether the safety layer is baseline, shadow or validated.
        # Everything else is `pqb consistency`.
        self.consistency_note = QLabel()
        self.consistency_note.setWordWrap(True)
        self.consistency_note.setStyleSheet(PANEL_STYLE)
        layout.addWidget(self.consistency_note)
        self.tabs.addTab(page, "Results")

    def _build_discovery(self):
        """Where the Quant Bridge's discovered, validated strategies show up."""
        page = QWidget()
        layout = QVBoxLayout(page)

        # The explanation above the table is long on purpose — it is the only
        # place the research layer says WHY a row is where it is. But it grows
        # with the pass, and on a laptop it pushed the rows themselves off the
        # bottom of the window with no way to get them back. So: a draggable
        # divider between prose and rows, plus a one-click collapse that gives
        # the whole tab to the table. Neither changes a single number.
        self.discovery_toggle = QPushButton("▾  Hide the explanation")
        self.discovery_toggle.setCheckable(True)
        self.discovery_toggle.setChecked(True)
        self.discovery_toggle.setFlat(True)
        self.discovery_toggle.setStyleSheet(COLLAPSE_LINK_STYLE)
        self.discovery_toggle.setToolTip(
            "Hide the explanation and give the whole tab to the table. "
            "You can also drag the divider between them.")
        self.discovery_toggle.clicked.connect(self._toggle_discovery_note)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self.discovery_toggle)
        bar.addStretch()
        layout.addLayout(bar)

        self.discovery_note = QLabel()
        self.discovery_note.setWordWrap(True)
        self.discovery_note.setAlignment(ALIGN_TOP)
        self.discovery_note.setStyleSheet(
            PANEL_STYLE)
        # Scrolled rather than clipped: shrinking the prose pane must never be
        # able to hide a sentence with no way to reach it.
        self.discovery_scroll = QScrollArea()
        self.discovery_scroll.setWidget(self.discovery_note)
        self.discovery_scroll.setWidgetResizable(True)
        self.discovery_scroll.setStyleSheet("QScrollArea{border:none;}")
        self.discovery_table = Table(
            ["Trading rule", "Ver", "Status", "Why not trading",
             "Attacked", "Why researching",
             "Priority", "Family evid.", "Motif", "Family score",
             "Evidence", "OOS win",
             "OOS trades", "OOS mkts", "OOS expect", "In-sample",
             "Last validated"])
        # DRILL-DOWN rather than thirty columns (§22). The family layer has
        # fifteen things to say about a row and the main table has room for
        # two of them; putting the rest behind a double-click keeps the board
        # readable, which was the operator's actual complaint about it.
        self._discovery_rows: list = []
        self.discovery_table.cellDoubleClicked.connect(self._show_family_detail)

        self.discovery_split = QSplitter(VERTICAL)
        self.discovery_split.addWidget(self.discovery_scroll)
        self.discovery_split.addWidget(self.discovery_table)
        self.discovery_split.setHandleWidth(10)
        self.discovery_split.setChildrenCollapsible(True)
        # The table is the thing that must survive a resize of the window; the
        # prose keeps whatever the operator dragged it to.
        self.discovery_split.setStretchFactor(0, 0)
        self.discovery_split.setStretchFactor(1, 1)
        self.discovery_split.setSizes([300, 420])
        self.discovery_split.setStyleSheet(SPLIT_HANDLE_STYLE)
        layout.addWidget(self.discovery_split)
        self.tabs.addTab(page, "Discovery")

    def _toggle_discovery_note(self):
        """Collapse or restore the explanation pane.

        Remembers the height it was dragged to, so re-showing it does not
        silently reset a layout the operator chose.
        """
        showing = self.discovery_toggle.isChecked()
        if not showing:
            sizes = self.discovery_split.sizes()
            if sizes and sizes[0] > 0:
                self._discovery_note_height = sizes[0]
            self.discovery_scroll.hide()
            self.discovery_toggle.setText("▸  Show the explanation")
        else:
            self.discovery_scroll.show()
            keep = getattr(self, "_discovery_note_height", 300)
            total = sum(self.discovery_split.sizes()) or 720
            self.discovery_split.setSizes([keep, max(total - keep, 120)])
            self.discovery_toggle.setText("▾  Hide the explanation")

    def _show_family_detail(self, row: int, _col: int = 0):
        """Everything the family/motif layer knows about one candidate.

        Every number here is read from the row the research pass wrote. The
        panel says, in its own words, that none of it is evidence about this
        candidate — because a screen that shows "family replication 100%" next
        to "OOS trades 4" will otherwise be read as a promotion, which is the
        one thing this layer must never be.
        """
        if not (0 <= row < len(self._discovery_rows)):
            return
        s = self._discovery_rows[row]
        motif_name = str(s.get("motif") or "")
        lines = [
            f"<b>{(s.get('describe') or s.get('signature') or 'rule')[:90]}</b>",
            f"Status <b>{s.get('status', 'new')}</b> · version "
            f"v{int(s.get('version', 1))} · research priority "
            f"{float(s.get('priority', 0)):.2f}",
            "",
            "<b>THIS CANDIDATE'S OWN EVIDENCE</b> — the only thing that can "
            "ever validate it:",
            f"&nbsp;&nbsp;{int(s.get('oosTrades', 0))} OOS trades across "
            f"{int(s.get('oosMarkets', 0))} independent markets · expectancy "
            f"{float(s.get('oosExpectancy', 0)):+.4f} · evidence score "
            f"{float(s.get('evidence', 0)):.3f}",
            f"&nbsp;&nbsp;Blocking: "
            f"{'; '.join(s.get('blockers') or []) or 'nothing'}",
            "",
            "<b>FAMILY / MOTIF — RESEARCH EVIDENCE ONLY</b>. This is what the "
            "structural class has done across OTHER candidates on OTHER "
            "markets. It changes where research effort goes and cannot "
            "change the status above, add a trade to the record, or authorise "
            "a trade:",
            f"&nbsp;&nbsp;Motif: <b>{motif_name or '—'}</b>",
            f"&nbsp;&nbsp;Family research score "
            f"{float(s.get('familyResearchScore', 0)):.3f} · priority weight "
            f"x{float(s.get('motifWeight', 1)):.2f}",
            f"&nbsp;&nbsp;Replication "
            f"{float(s.get('familyReplication', 0)):.0%} over "
            f"{int(s.get('familyIndependentCandidates', 0))} INDEPENDENT "
            f"candidate(s) — candidates sharing markets are counted once — "
            f"across {int(s.get('familyIndependentMarkets', 0))} "
            "non-overlapping market(s)",
            f"&nbsp;&nbsp;Hypothesis-family ledger (this signature, all "
            f"versions): {int(s.get('familyTrades', 0))} trades / "
            f"{int(s.get('familyMarkets', 0))} markets over "
            f"{int(s.get('familyVersions', 1))} version(s), expectancy "
            f"{float(s.get('familyExpectancy', 0)):+.4f}",
        ]
        if s.get("familyFailureMotif"):
            lines.append(f"&nbsp;&nbsp;Recurring failure motif: "
                         f"<b>{s['familyFailureMotif']}</b>")
        if s.get("whyFamilyElevated"):
            lines += ["", "<b>Why this family gained priority</b>",
                      "&nbsp;&nbsp;" + str(s["whyFamilyElevated"])]
        if s.get("whyFamilyDeprioritised"):
            lines += ["", "<b>Why this family lost priority</b>",
                      "&nbsp;&nbsp;" + str(s["whyFamilyDeprioritised"])]
        scale = (self.reader.motifs() or {}).get("scale") or {}
        if scale:
            lines += ["", "<b>Scale of the search behind this</b> — a "
                          "best-of-N result is not a finding until it "
                          "replicates:",
                      f"&nbsp;&nbsp;{int(scale.get('motifsExamined', 0))} "
                      f"structural motif(s) examined, "
                      f"{int(scale.get('motifsWithStanding', 0))} with enough "
                      f"record to have an opinion, "
                      f"{int(scale.get('motifsReplicated', 0))} replicated on "
                      "independent evidence, "
                      f"{int(scale.get('motifFailures', 0))} recurring "
                      "failure motif(s)."]
        box = QMessageBox(self)
        box.setWindowTitle("Family & motif detail")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText("<br>".join(lines))
        box.exec()

    def _fill_discovery(self):
        try:
            d = self.reader.discovery()
        except Exception:                                # noqa: BLE001
            return
        status = d.get("status") or {}
        strategies = d.get("strategies") or []
        ready = int(d.get("tokensReady", 0))

        parts: list[str] = []
        if status.get("running"):
            phase = status.get("phase", "")
            note = status.get("note", "")
            if phase == "backfill":
                parts.append("<b>Working right now: building the historical "
                             "dataset.</b> " + note)
            elif phase == "research":
                parts.append("<b>Working right now: researching strategies "
                             "over the historical data.</b> " + note)
            else:
                parts.append("<b>Discovery is running…</b> " + note)
        if strategies:
            by_status: dict[str, int] = {}
            for s in strategies:
                key = str(s.get("status", "new"))
                by_status[key] = by_status.get(key, 0) + 1
            tradable = (by_status.get("validated", 0)
                        + by_status.get("high_confidence", 0))
            counts = ", ".join(
                f"{by_status[k]} {label}" for k, label in
                (("high_confidence", "high-confidence"),
                 ("validated", "validated"), ("watch", "on watch"),
                 ("validating", "validating"), ("new", "new"),
                 ("degraded", "degraded"), ("retired", "retired"),
                 ("rejected", "rejected")) if by_status.get(k))
            parts.append(
                f"<b>Strategy library: {len(strategies)} on permanent record "
                f"({counts}) — {tradable} may trade.</b> "
                "This is a LIBRARY, not a leaderboard: each hourly pass adds "
                "evidence, it never wipes the slate. Evidence accumulates one "
                "independent market at a time (a market never testifies "
                "twice), a changed rule becomes a new version that earns "
                "trust from zero, and a validated strategy that sours steps "
                "down gradually — watch, degraded, retired — with retired "
                "rows kept forever. Every win rate is shown with the trade "
                "count behind it, because 98% of 8 trades is not 98% of 500.")
            attacked = sum(1 for s in strategies
                           if str(s.get("adversarialVerdict") or "")
                           not in ("", "NOT_ATTACKED"))
            broken = sum(1 for s in strategies
                         if s.get("adversarialVerdict") == "BROKEN")
            parts.append(
                "<b>\"Attacked\" and \"Why researching\" are the research "
                f"layer talking, not the validator.</b> {attacked} candidate"
                f"(s) have been deliberately attacked so far, {broken} broke. "
                "The battery only attacks records that look GOOD — it removes "
                "the best market and re-sums, splits the evidence by time, by "
                "book depth and by market, charges higher costs, checks "
                "whether the inverse pays just as well, and runs a random-"
                "entry control to see whether the signal beats simply holding "
                "for the same time. The percentage is how much of that "
                "battery the record was deep enough to answer: SURVIVED at "
                "40% means most questions could not be asked yet, not that it "
                "passed them. <b>None of this can promote or reject "
                "anything</b> — a BROKEN row keeps its full record and stays "
                "in the library; it simply drops down the research queue. "
                "Only the OOS evidence columns to the right decide status.")
            scale = (self.reader.motifs() or {}).get("scale") or {}
            if scale.get("motifsExamined"):
                parts.append(
                    "<b>\"Motif\" and \"Family score\" are the family layer — "
                    "also research, also unable to promote anything.</b> It "
                    "looks for STRUCTURE that recurs across otherwise "
                    "different strategies (holding period, direction, entry "
                    "timing, evidence shape) and asks whether the recurrence "
                    "survives independent testing. "
                    f"{int(scale.get('motifsExamined', 0))} structural "
                    f"motif(s) examined so far, "
                    f"{int(scale.get('motifsWithStanding', 0))} have enough "
                    "record to have an opinion at all, "
                    f"{int(scale.get('motifsReplicated', 0))} replicated on "
                    "independent evidence, "
                    f"{int(scale.get('motifFailures', 0))} are recurring "
                    "FAILURE structures the queue now avoids. Two strategies "
                    "tested on the same markets count as ONE confirmation, "
                    "which is why the score shows independent confirmations "
                    "(<i>ic</i>) rather than candidate counts. A strong "
                    "family never lends a single trade to a new candidate — "
                    "it only decides what gets looked at sooner. Double-click "
                    "any row for the full family panel."
                    + (f" Strongest so far: {scale.get('motifStrongest')}."
                       if scale.get("motifStrongest") else ""))
        else:
            parts.append(
                "<b>The strategy library is empty so far.</b> The Quant Bridge "
                "searches for entries automatically once markets have enough "
                "history; until something survives frozen validation on unseen "
                "markets, the bot stays in learning mode.")
        if status.get("skippedReason"):
            parts.append("Last discovery pass: " + str(status["skippedReason"]))
        elif status.get("lastRun"):
            parts.append(
                f"Last pass examined {int(status.get('candidates', 0))} candidate "
                f"rules and kept {int(status.get('accepted', 0))}. "
                "\"Nothing kept\" is a real result, not a failure — it means "
                "nothing held up out of sample.")
        funnel = status.get("funnel") or {}
        if funnel:
            # The pipeline, stage by stage — an empty board must explain
            # itself instead of looking broken.
            steps = [("trades", "rawTrades"), ("series", "seriesExported"),
                     ("researched", "seriesResearched"),
                     ("candidates", "rankedCandidates"),
                     ("cross-market", "crossMarketCandidates"),
                     ("registered", "registeredThisPass"),
                     ("validated+tradable", "tradable")]
            chain = " → ".join(f"{int(funnel.get(key) or 0):,} {label}"
                               for label, key in steps)
            parts.append("<b>Last pass, stage by stage:</b> " + chain)
            if funnel.get("zeroedAt"):
                parts.append("First empty stage — <b>"
                             + str(funnel.get("zeroedAt")) + "</b>: "
                             + str(funnel.get("zeroedWhy") or ""))
            health = funnel.get("health") or {}
            if health:
                blocked = ", ".join(
                    f"{v} × {k}" for k, v in
                    list((health.get("blockedBy") or {}).items())[:4])
                # The pool BACKLOG must be visible here: a first pass shows
                # a small pool with hundreds still queued, and without the
                # queue count that honest ramp-up reads as "broken".
                unprocessed = int(funnel.get("poolUnprocessed") or 0)
                thin = int(funnel.get("poolKnownThin") or 0)
                backlog = (f" Pool backlog: {unprocessed} settled markets "
                           "still queued for processing (grows the pool "
                           "next passes)." if unprocessed else
                           (" Pool backlog fully processed"
                            + (f" ({thin} markets had too little in-band "
                               "data to use)." if thin else ".")))
                parts.append(
                    "<b>Research health:</b> "
                    f"{int(health.get('eligiblePoolMarkets', 0))} markets in "
                    "the OOS pool, "
                    f"{int(health.get('oosAllocationsThisPass', 0))} replays "
                    "allocated this pass; independent OOS markets per "
                    f"candidate avg {health.get('avgOosMarkets', 0)} / median "
                    f"{health.get('medianOosMarkets', 0)} / max "
                    f"{health.get('maxOosMarkets', 0)}."
                    + (f" Blocked by: {blocked}." if blocked else "")
                    + backlog)
        if funnel:
            # MARKET SUPPLY. The single biggest constraint on this whole
            # machine, and previously invisible: a small pool looked like a
            # small dataset rather than like most of the dataset being
            # thrown away at the door.
            considered = int(funnel.get("seriesConsidered") or 0)
            if considered:
                rejected = funnel.get("seriesRejectedBy") or {}
                reasons = "; ".join(
                    f"{count} {reason.replace('_', ' ')}"
                    for reason, count in sorted(rejected.items()))
                parts.append(
                    "<b>Market supply:</b> "
                    f"{int(funnel.get('settledMarkets') or 0)} settled "
                    "markets known; this pass considered "
                    f"{considered} and admitted "
                    f"{int(funnel.get('seriesAdmitted') or 0)}."
                    + (f" Rejected: {reasons}." if reasons else "")
                    + " A market is admitted on how much TRADING it "
                    "contains, not on how long it stayed open — a market "
                    "that settled in ten minutes with thousands of trades "
                    "is rich data, and used to be discarded for being "
                    "short.")

            # WHERE THE REPLAY BUDGET WENT. The gap between "replays
            # allocated" and "evidence gained" is where the market supply
            # was being lost, and it was not visible anywhere before.
            allocated = int(funnel.get("oosAllocations") or 0)
            if allocated:
                zero = int(funnel.get("zeroTradeAttempts") or 0)
                failed = int(funnel.get("replayFailures") or 0)
                parts.append(
                    "<b>Where the research budget went:</b> "
                    f"{allocated} replays — "
                    f"{int(funnel.get('newIndependentEvents') or 0)} produced "
                    f"evidence, {zero} never fired, {failed} failed. "
                    "A rule that never fires in a market has NOT been tested "
                    "there: that market stays available instead of being "
                    "used up, which is the fix for candidates quietly "
                    "consuming the whole pool without proving anything. "
                    f"{int(funnel.get('forwardEvidenceMarkets') or 0)} "
                    "market(s) of evidence are true walk-forward (the market "
                    "began after the rule was found) rather than merely "
                    "unseen.")

            # ALLOCATION SPLIT. Explains why an unproven candidate is being
            # looked at at all.
            if int(funnel.get("allocatedTotal") or 0):
                parts.append(
                    "<b>Who got researched:</b> "
                    f"{int(funnel.get('allocatedExploration') or 0)} never "
                    "tested before, "
                    f"{int(funnel.get('allocatedNearMiss') or 0)} near "
                    "misses, "
                    f"{int(funnel.get('allocatedExploitation') or 0)} on "
                    "their existing record. A fixed share of every pass is "
                    "reserved for candidates with no evidence yet — without "
                    "it, a candidate with nothing to show never gets looked "
                    "at, so it never gets anything to show.")

            # FEATURE COMPATIBILITY + QUARANTINE.
            quarantined = int(funnel.get("quarantinedThisPass") or 0)
            refused = int(funnel.get("skippedFeatureIncompatible") or 0)
            if quarantined or refused or funnel.get("featuresKnown"):
                parts.append(
                    "<b>Feature compatibility:</b> "
                    f"{int(funnel.get('featuresUsableInValidation') or 0)} of "
                    f"{int(funnel.get('featuresKnown') or 0)} measurements "
                    "actually exist and vary in the data used for "
                    "validation."
                    + (f" {refused} new rule(s) refused and {quarantined} "
                       "existing one(s) quarantined this pass."
                       if (refused or quarantined) else "")
                    + " A rule built on a measurement that is missing or "
                    "frozen during validation can never fire there — so it "
                    "is never tested, never rejected, and would sit on the "
                    "board looking merely unproven forever.")

            # META-DISCOVERY.
            if funnel.get("metaStructures"):
                if funnel.get("metaHasOpinion"):
                    parts.append(
                        "<b>What kinds of research are working:</b> "
                        f"{int(funnel.get('metaStructuresSteering') or 0)} of "
                        f"{int(funnel.get('metaStructuresWithStanding') or 0)}"
                        " research approaches now have enough of a track "
                        "record to steer effort — strongest so far: "
                        f"{funnel.get('metaStrongest') or '—'}. This moves "
                        "only what gets LOOKED AT next; it can never make "
                        "anything validated.")
                else:
                    parts.append(
                        "<b>What kinds of research are working:</b> no "
                        "approach has enough of a track record to steer "
                        "effort yet. That is the honest answer rather than a "
                        "fault — guessing early would just amplify noise.")

            # THE HYPOTHESIS LAYER.
            if funnel.get("hypothesesTotal"):
                parts.append(
                    "<b>Cross-source agreement:</b> "
                    f"{int(funnel.get('hypothesesTotal') or 0)} proposed "
                    "market relationships tracked, "
                    f"{int(funnel.get('convergenceGroups') or 0)} case(s) "
                    "where different research methods describe the same "
                    "thing — the strongest backed by "
                    f"{int(funnel.get('convergenceIndependentMax') or 0)} "
                    "genuinely INDEPENDENT method(s). "
                    f"{int(funnel.get('adversarialTested') or 0)} have been "
                    "attacked on purpose, "
                    f"{int(funnel.get('adversarialRejected') or 0)} did not "
                    "survive, and "
                    f"{int(funnel.get('inverseWinners') or 0)} turned out to "
                    "work better BACKWARDS. Agreement between methods only "
                    "decides what is investigated harder — it is never "
                    "counted as proof, and nothing here can trade.")

        wallet_research = (status.get("funnel") or {}).get(
            "walletBehavior") or {}
        if wallet_research:
            census = wallet_research.get("library") or {}
            in_flight = (census.get("new", 0) + census.get("validating", 0)
                         + census.get("watch", 0))
            proven = (census.get("validated", 0)
                      + census.get("high_confidence", 0))
            convergent = int(wallet_research.get("convergent") or 0)
            parts.append(
                "<b>Wallet research:</b> "
                f"{int(wallet_research.get('walletsConsidered', 0))} "
                "top wallet(s) studied "
                f"({int(wallet_research.get('walletsEligible', 0))} with "
                "enough settled history) → "
                f"{int(wallet_research.get('settledObservations', 0))} "
                "reconstructed entries → "
                f"{int(wallet_research.get('cellsFormed', 0))} repeating-"
                "behavior patterns → "
                f"{int(wallet_research.get('kept', 0))} hypothesis(es) this "
                f"pass ({int(wallet_research.get('duplicatesMerged', 0))} "
                "duplicate wallet(s) merged into shared patterns). "
                f"On the permanent record: "
                f"{int(wallet_research.get('libraryTotal', 0))} wallet-derived "
                + ("strategy" if int(wallet_research.get("libraryTotal", 0))
                   == 1 else "strategies")
                + f" — {in_flight} gathering evidence, "
                f"{census.get('rejected', 0)} rejected, {proven} validated."
                + (f" {convergent} independently match a quant-discovered "
                   "rule (convergent discovery)." if convergent else "")
                + (f" Side switches: "
                   f"{int(wallet_research.get('switchObservations', 0))} "
                   "reconstructed → "
                   f"{int(wallet_research.get('switchCells', 0))} "
                   "conditional patterns → "
                   f"{int(wallet_research.get('keptSwitch', 0))} kept as "
                   "hypotheses — switching is studied as a CONDITION "
                   "(what precedes it), never assumed to be an edge."
                   if wallet_research.get("switchObservations") else "")
                + " A wallet's success only ever creates the hypothesis; "
                "validation happens on markets the wallet never traded, "
                "through the same gates as everything else.")
        parts.append(
            "It studies the PAST, not just what it watches live: settled "
            "markets' full trade histories (pulled by backfill) replay into "
            "series it can validate against known outcomes, and live capture "
            f"extends them forward. {ready} live market(s) also have 200+ "
            "snapshots. Discovery runs automatically about once an hour while "
            "the bot is on — nothing to launch by hand.")
        self.discovery_note.setText("<br><br>".join(parts))

        rows = []
        colours: dict[int, str] = {}
        status_names = {"high_confidence": "HIGH-CONFIDENCE",
                        "validated": "VALIDATED",
                        "watch": "WATCH",
                        "validating": "validating",
                        "new": "new",
                        "degraded": "degraded",
                        "retired": "retired",
                        "rejected": "rejected",
                        # pre-library records, shown as-is if ever loaded
                        "oos_testing": "validating",
                        "candidate": "new",
                        "failed_oos": "rejected"}
        for i, s in enumerate(strategies):
            status = str(s.get("status", "new"))
            trades = int(s.get("oosTrades", 0))
            last_ts = float(s.get("lastValidatedTs", 0) or 0)
            family_trades = int(s.get("familyTrades", 0))
            rows.append([
                (s.get("describe") or s.get("signature") or "rule")[:56],
                f"v{int(s.get('version', 1))}",
                status_names.get(status, status),
                # Why-not-validated: EVERY active blocker with its numeric
                # target, plus the next action for market-starved rows —
                # "INSUFFICIENT_O..." explains nothing.
                ("; ".join(s.get("blockers") or [])
                 + (" -> " + s["nextAction"] if s.get("nextAction") else "")
                 ) or str(s.get("maturity") or "") or "-",
                # WHAT DELIBERATE ATTACK FOUND. Shown next to the status it
                # cannot change, on purpose: a BROKEN row sitting beside
                # "validating" is the honest picture, and collapsing the two
                # into one column would be exactly the "AI score replaces
                # validation" the architecture forbids. "not attacked" is a
                # real state — the battery declines candidates whose record
                # is too thin to attack rather than passing them.
                _attack_cell(s),
                # WHY IT IS IN THE QUEUE. The research layer's own sentence,
                # verbatim. If this column is ever empty for a candidate
                # receiving research, the allocator has stopped explaining
                # itself and that is a bug worth seeing.
                # 96, not 70: the real sentences run to ~93 characters
                # ("...because 6 independent OOS markets; also untested
                # outside one category"), and cutting at 70 removed the
                # clause that carried the actual reason.
                (str(s.get("whyMoreResearch") or s.get("whyStopped") or "-")
                 )[:96],
                # Research priority: where the next holdout evaluation
                # slots go. Allocation only — it cannot promote or trade.
                f"{float(s.get('priority', 0)):.2f}",
                # The HYPOTHESIS-family ledger: evidence across all
                # versions, market-deduplicated. Version columns to the
                # right stay the atomic record.
                (f"{family_trades}t/{int(s.get('familyMarkets', 0))}m"
                 f"×{int(s.get('familyVersions', 1))}v"
                 if family_trades else "-"),
                # THE MOTIF. The structural class this row belongs to, named.
                # Research evidence, never validation evidence — double-click
                # the row for the panel that says so at length.
                (str(s.get("motif") or "-").split("=")[-1])[:22],
                # The Family Research Score, with the independent-confirmation
                # count beside it. Never the raw candidate count: four
                # candidates replayed on the same three markets are one
                # confirmation, and a column that showed "4" there would be
                # the exact double-count this layer exists to prevent.
                (f"{float(s.get('familyResearchScore', 0)):.2f} "
                 f"({int(s.get('familyIndependentCandidates', 0))}ic)"
                 if s.get("familyResearchScore") else "-"),
                # The composite score: sample x breadth x confidence x
                # diversification. What actually ranks the board. Three
                # decimals: a small REAL score must not display as 0.00.
                f"{float(s.get('evidence', 0)):.3f}",
                # Win rate NEVER shown without its sample size beside it.
                f"{float(s.get('oosWin', 0)) * 100:.0f}%" if trades else "-",
                str(trades),
                str(int(s.get("oosMarkets", 0))),
                f"{float(s.get('oosExpectancy', 0)):+.4f}" if trades else "-",
                f"{float(s.get('winRate', 0)) * 100:.0f}% "
                f"({float(s.get('score', 0)):.2f})",
                time.strftime("%Y-%m-%d %H:%M",
                              time.localtime(last_ts)) if last_ts else "-",
            ])
            if status in ("validated", "high_confidence"):
                colours[i] = "#1f9d55"
            elif status == "watch":
                colours[i] = "#b45309"
            elif status in ("degraded", "retired", "rejected"):
                colours[i] = "#6b7280"
        self._discovery_rows = list(strategies)
        self.discovery_table.fill(rows, colours)

    def _build_table_tab(self, name: str, headers: list[str]):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel()
        note.setStyleSheet(f"color:{MUTED};")
        note.setWordWrap(True)
        table = Table(headers)
        layout.addWidget(note)
        layout.addWidget(table)
        setattr(self, f"_tbl_{name[:4].lower()}", table)
        setattr(self, f"_note_{name[:4].lower()}", note)
        self.tabs.addTab(page, name)

    def _build_settings(self):
        """Every setting a person should be able to change, with plain names.

        Deliberately not every setting in the file — hosts, paths and the two
        live-trading flags are not things to change by accident from a window.
        """
        from . import settings as settings_mod
        page = QWidget()
        layout = QVBoxLayout(page)

        note = QLabel(
            f"<b>{settings_mod.NOTE}</b><br><br>"
            "Saved straight to your settings file, keeping all its notes. "
            "The bot picks changes up when you next start it — press Stop "
            "then Start to apply them now.")
        note.setWordWrap(True)
        note.setStyleSheet(
            PANEL_STYLE)
        layout.addWidget(note)

        # Settings where 0 means OFF, shown with an explicit on/off switch so
        # the operator flips a checkbox rather than typing a zero. OFF writes 0
        # to the file; the bot's own semantics ("0 = disabled") are unchanged.
        toggled_paths = {                     # value shown when re-enabled (%)
            "engine.portfolio.max_drawdown_pct": 30.0,
            "engine.portfolio.reserve_cash_fraction": 5.0,
        }

        self.setting_widgets = {}
        self.setting_toggles = {}
        for group in settings_mod.groups():
            box = QGroupBox(group)
            grid = QGridLayout(box)
            row = 0
            for spec in settings_mod.EDITABLE:
                if spec.group != group:
                    continue
                name = QLabel(spec.label)
                name.setToolTip(spec.help)
                if spec.kind == "bool":
                    # A pure switch: no number to type, so no spinbox.
                    check = QCheckBox("On")
                    helper = QLabel(spec.help)
                    helper.setStyleSheet(f"color:{MUTED};")
                    helper.setWordWrap(True)
                    grid.addWidget(name, row, 0)
                    grid.addWidget(check, row, 1)
                    grid.addWidget(helper, row, 2)
                    grid.setColumnStretch(2, 1)
                    self.setting_widgets[spec.path] = (spec, check)
                    row += 1
                    continue
                field = QDoubleSpinBox()
                if spec.kind == "int":
                    field.setDecimals(0); field.setSingleStep(1)
                elif spec.kind == "percent":
                    # 3 decimals so a value as small as 0.001% is representable;
                    # 1 decimal silently rounded it away and looked like the box
                    # "defaulted back".
                    field.setDecimals(3); field.setSingleStep(0.1)
                    field.setSuffix(" %")
                elif spec.kind == "money":
                    field.setDecimals(2); field.setSingleStep(0.25)
                    field.setPrefix("$")
                else:
                    field.setDecimals(2); field.setSingleStep(0.05)
                low, high = spec.low, spec.high
                if spec.kind == "percent":
                    low, high = low * 100, high * 100
                field.setRange(low, high)
                helper = QLabel(spec.help)
                helper.setStyleSheet(f"color:{MUTED};")
                helper.setWordWrap(True)
                grid.addWidget(name, row, 0)
                if spec.path in toggled_paths:
                    # ON/OFF beside the value. OFF greys the field and saves 0.
                    toggle = QCheckBox("On")
                    toggle.toggled.connect(field.setEnabled)
                    cell = QWidget()
                    cell_layout = QHBoxLayout(cell)
                    cell_layout.setContentsMargins(0, 0, 0, 0)
                    cell_layout.addWidget(toggle)
                    cell_layout.addWidget(field)
                    grid.addWidget(cell, row, 1)
                    self.setting_toggles[spec.path] = (
                        toggle, toggled_paths[spec.path])
                else:
                    grid.addWidget(field, row, 1)
                grid.addWidget(helper, row, 2)
                grid.setColumnStretch(2, 1)
                self.setting_widgets[spec.path] = (spec, field)
                row += 1
            layout.addWidget(box)

        buttons = QHBoxLayout()
        self.settings_status = QLabel("")
        buttons.addWidget(self.settings_status)
        buttons.addStretch()
        reload_btn = QPushButton("Undo changes")
        reload_btn.clicked.connect(self.load_settings)
        save_btn = QPushButton("Save settings")
        save_btn.setStyleSheet(
            f"background:{GREEN}; color:white; font-weight:bold; padding:6px 14px;")
        save_btn.clicked.connect(self.save_settings)
        buttons.addWidget(reload_btn)
        buttons.addWidget(save_btn)
        layout.addLayout(buttons)
        layout.addStretch()
        self.tabs.addTab(page, "Settings")
        self.load_settings()

    def load_settings(self):
        from . import settings as settings_mod
        try:
            values = settings_mod.read(self.config_path)
        except Exception as exc:                      # noqa: BLE001
            self.settings_status.setText(f"Could not read settings: {exc}")
            return
        for path, (spec, field) in self.setting_widgets.items():
            if path not in values:
                continue
            value = values[path]
            if spec.kind == "bool":
                field.setChecked(bool(value))
                continue
            shown = value * 100 if spec.kind == "percent" else value
            if path in self.setting_toggles:
                toggle, default_shown = self.setting_toggles[path]
                on = value > 0
                toggle.setChecked(on)
                field.setEnabled(on)
                # When off, show the value it would come back ON at rather
                # than a meaningless 0.
                field.setValue(shown if on else default_shown)
            else:
                field.setValue(shown)
        self.settings_status.setText("")

    def save_settings(self):
        from . import settings as settings_mod
        payload = {}
        for path, (spec, field) in self.setting_widgets.items():
            if spec.kind == "bool":
                payload[path] = field.isChecked()
                continue
            if path in self.setting_toggles and \
                    not self.setting_toggles[path][0].isChecked():
                payload[path] = 0.0        # OFF is written as 0 = disabled
                continue
            value = field.value()
            payload[path] = value / 100 if spec.kind == "percent" else value
        try:
            changed = settings_mod.write(self.config_path, payload)
        except Exception as exc:                      # noqa: BLE001
            self.settings_status.setText(f"Could not save: {exc}")
            return
        if not changed:
            self.settings_status.setText("Nothing changed.")
            return
        self.settings_status.setStyleSheet(f"color:{GREEN};")
        self.settings_status.setText(
            f"Saved {len(changed)} change(s). Stop and Start the bot to apply.")

    def _build_activity(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel("Every decision the bot has made, newest first.")
        note.setStyleSheet(f"color:{MUTED};")
        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setFont(QFont("Consolas", 9))
        layout.addWidget(note)
        layout.addWidget(self.activity)
        self.tabs.addTab(page, "Activity")

    # -- actions -------------------------------------------------------------

    def on_start(self):
        self.bot.start()
        # Reflect the click before anything slow happens, so the button never
        # sits enabled after being pressed.
        self.sync_run_state()
        self.statusBar().showMessage("Bot starting — first numbers in a minute…")

    def on_stop(self):
        self.bot.request_stop()
        self.sync_run_state()

    def sync_run_state(self):
        """Make the buttons and the label agree with reality, four times a second.

        This is the whole answer to "I pressed Stop and nothing happened": the
        stop is a request, the bot finishes the cycle it is in, and every
        moment of that is now shown rather than hidden behind a frozen window.
        """
        self.bot.poll()
        state = self.bot.state

        self.start_btn.setEnabled(state == "stopped")
        # Disabled the instant Stop is pressed — pressing it twice does nothing
        # useful and invites the conclusion that it is broken.
        self.stop_btn.setEnabled(state in ("running", "starting"))

        if state == "stopped":
            text, colour = "not running", MUTED
        elif state == "starting":
            text, colour = "starting…", AMBER
        elif state == "stopping":
            waited = int(self.bot.stopping_for)
            text = f"stopping… {waited}s"
            colour = AMBER
            if waited >= int(STOP_GRACE_SECONDS):
                text = f"stopping… {waited}s (forcing)"
                colour = RED
        else:
            text, colour = "running", GREEN
        self.run_state.setText(f"●  {text}")
        self.run_state.setStyleSheet(f"color:{colour};")

        if state == "stopping":
            self.statusBar().showMessage(
                "Stopping — it finishes the cycle it is in so nothing is lost. "
                "Usually a few seconds.")
        elif state == "stopped" and self._was_stopping:
            self.statusBar().showMessage("Bot stopped.")
        self._was_stopping = state == "stopping"

    def on_panic(self):
        box = QMessageBox(self)
        box.setWindowTitle("Stop trading")
        box.setText("Stop the bot placing any new orders?")
        box.setInformativeText(
            "This takes effect within seconds, even while it is running.\n\n"
            "'Stop and close' also sells every open position.")
        halt = box.addButton("Stop new orders", QMessageBox.ButtonRole.AcceptRole)
        flat = box.addButton("Stop and close everything",
                             QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is halt or clicked is flat:
            path = self.cfg.kill_switch_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("flatten" if clicked is flat else "halt",
                            encoding="utf-8")
            # The file is an instruction; the RUNNING bot is what carries it
            # out. Saying so here is what was missing the day the operator
            # pressed close-everything on a stopped bot, saw nothing happen,
            # and reasonably concluded the button was broken.
            running = self.bot.running or getattr(self, "_last_o", {}).get("running")
            if clicked is flat and not running:
                QMessageBox.information(
                    self, "One more step",
                    "The close-out is saved, but the bot is OFF - it only "
                    "sells while running.\n\nPress 'Start bot': it will close "
                    "every position within a few seconds. Press 'Resume "
                    "trading' only after the open positions reach 0.")
                self.statusBar().showMessage(
                    "Close-out saved. Press 'Start bot' to carry it out.")
            elif clicked is flat:
                self.statusBar().showMessage(
                    "Closing every position now - watch Open positions reach "
                    "0, then press 'Resume trading'.")
            else:
                self.statusBar().showMessage(
                    "Trading stopped. The green 'Resume trading' button "
                    "brings it back.")

    def on_resume(self):
        """Clear the kill switch — the same thing 9-RESUME.bat does.

        Deliberately does NOT touch a reconciliation HALT: that one means the
        bot's records disagreed with a real exchange, and clearing it deserves
        a review (`pqb.cli resume --force`), not a green button.
        """
        path = self.cfg.kill_switch_path
        try:
            body = path.read_text(encoding="utf-8").strip().lower() \
                if path.exists() else ""
        except OSError:
            body = ""
        open_now = int(getattr(self, "_last_o", {}).get("open_positions") or 0)
        if "flatten" in body and open_now > 0:
            # Resuming mid-close-out cancels it — the exact silent no-op that
            # left positions open the first time. Make it a choice, not a trap.
            box = QMessageBox(self)
            box.setWindowTitle("Positions are still open")
            box.setText(f"{open_now} position(s) have not been closed yet.")
            box.setInformativeText(
                "Resuming now CANCELS the close-out and keeps them.\n\n"
                "To finish closing first: leave this stop in place with the "
                "bot running, watch Open positions reach 0, then resume.")
            keep = box.addButton("Keep closing", QMessageBox.ButtonRole.RejectRole)
            box.addButton("Resume anyway", QMessageBox.ButtonRole.AcceptRole)
            box.exec()
            if box.clickedButton() is keep:
                return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            self.statusBar().showMessage(f"Could not clear the stop: {exc}")
            return
        self.statusBar().showMessage(
            "Trading allowed again. The bot picks it up within a cycle.")
        self.refresh()

    def on_reset(self):
        """Start the simulation over at its opening balance."""
        # BOTH kinds of running bot hold the journal open: the one this window
        # spawned AND one left running by an earlier window ("Leave it
        # running" on close). Missing the second was exactly the operator's
        # "Could not remove journal.sqlite3 - in use by another process".
        running = self.bot.running or bool(
            getattr(self, "_last_o", {}).get("running"))
        if running:
            QMessageBox.information(
                self, "Stop the bot first",
                "A bot is running (possibly started from an earlier window) "
                "and holds the account files open - the reset cannot remove "
                "them.\n\nPress 'Stop bot' - it reaches a bot from any window "
                "- wait for 'stopped', then press Reset again.")
            return
        box = QMessageBox(self)
        box.setWindowTitle("Reset the simulated account")
        box.setText("Start over with a fresh $100?")
        box.setInformativeText(
            "This clears the simulated account: balance, open positions, "
            "closed trades, decision history and the trade-size progression.\n\n"
            "It KEEPS everything the bot has learned about the market - wallet "
            "history, rankings and discovered strategies.\n\nThis cannot be "
            "undone.")
        wipe = box.addButton("Reset to $100",
                             QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not wipe:
            return
        try:
            removed = perform_reset(self.cfg)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Could not reset", str(exc))
            return
        self.statusBar().showMessage(
            "Fresh account. Press 'Start bot' to begin again at "
            f"${self.cfg.mode.paper_starting_balance:,.2f}."
            + (f"  (cleared: {', '.join(removed)})" if removed else ""))
        self.refresh()

    def closeEvent(self, event):
        if self.bot.running:
            box = QMessageBox(self)
            box.setWindowTitle("The bot is still running")
            box.setText("Stop the bot before closing?")
            box.setInformativeText(
                "Leave it running and it keeps trading in the background.\n"
                "It only learns while it is on.")
            stop = box.addButton("Stop it", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Leave it running", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is stop:
                self.bot.stop()
        event.accept()

    # -- refresh -------------------------------------------------------------

    def _timed_refresh(self):
        """Refresh with a self-defending cadence.

        A refresh that takes longer than its own interval is how a window
        walks into "Not Responding" hour by hour: each tick queues behind the
        last against an ever-growing database. Measured here — if a refresh
        runs slow, the interval backs off (up to 30s) and recovers when reads
        get fast again, so the GUI always has more breathing room than the
        work costs.
        """
        started = time.time()
        self.refresh()
        cost = time.time() - started
        interval = self.timer.interval()
        if cost * 1000 > interval * 0.5 and interval < 30_000:
            self.timer.setInterval(min(30_000, interval * 2))
        elif cost * 1000 < REFRESH_MS * 0.25 and interval > REFRESH_MS:
            self.timer.setInterval(max(REFRESH_MS, interval // 2))

    def refresh(self):
        try:
            o = self.reader.overview()
        except Exception as exc:                        # noqa: BLE001
            self.statusBar().showMessage(f"Could not read the data: {exc}")
            return
        # Kept for the button handlers: panic and resume need to know whether
        # the bot is on and how many positions are open, without re-querying.
        self._last_o = o

        live = o["mode"] == "LIVE"
        self.mode_label.setText(
            ("● LIVE — REAL MONEY" if live else "● SIMULATION — pretend money")
            + (f"   |   {o['markets']} markets" if o["markets"] else ""))
        self.mode_label.setStyleSheet(f"color:{RED if live else GREEN};")

        running = self.bot.running or o["running"]
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        # Visible only while the kill switch is armed, so the way back sits
        # right where the operator is looking for it.
        self.resume_btn.setVisible(bool(o["killed"]))

        profit = o["profit"]
        self.cards["value"].set(_money(o["equity"]),
                                f"started with {_money(o['start_balance'])}")
        self.cards["profit"].set(
            f"{'+' if profit >= 0 else ''}{_money(profit)}",
            f"{o['profit_pct']:+.1f}%   ·   fees paid {_money(o['fees_paid'])}",
            GREEN if profit > 0 else (RED if profit < 0 else ""))
        self.cards["positions"].set(str(o["open_positions"]),
                                    f"cash free {_money(o['cash'])}")
        self.cards["closed"].set(
            str(o["closed_trades"]),
            f"{o['win_rate']:.0f}% winners · {_money(o['closed_pnl'])}"
            if o["closed_trades"] else "none finished yet")
        self.cards["wallets"].set(f"{o['wallets_observed']:,}",
                                  "you configured none of these")
        self.cards["ranked"].set(f"{o['wallets_ranked']:,}",
                                 "scored on measured results")
        self.cards["unusual"].set(f"{o['anomalies']:,}", "see Unusual activity")
        target = f" · doubles at {_money(o['target'])}" if o.get("target") else ""
        self.cards["size"].set(_money(o["min_trade_size"]),
                               f"minimum per trade{target}")

        self.explain.setText(self._explain(o, running))
        self._fill_health()
        self._fill_previews()
        self._fill_tables()
        self._fill_results()
        self._fill_discovery()

        age = o["last_cycle_age"]
        self.statusBar().showMessage(
            f"Updated {int(age)}s ago · {o['cycles']} cycles · "
            f"{o['errors']} errors in the last cycle"
            if age is not None else "Waiting for the bot's first cycle…")

    def _explain(self, o: dict, running: bool) -> str:
        if o["halted"]:
            return ("<b style='color:#c53030'>Trading is halted.</b> The bot "
                    "found a disagreement between its own records and the "
                    "exchange, and stopped rather than trade on it. Ask your "
                    "developer to review before resuming.")
        if o["killed"]:
            try:
                body = self.cfg.kill_switch_path.read_text(
                    encoding="utf-8").strip().lower()
            except OSError:
                body = ""
            open_now = int(o.get("open_positions") or 0)
            if "flatten" in body and open_now > 0:
                if not running:
                    return ("<b style='color:#b7791f'>Close-out is waiting for "
                            "the bot.</b> You asked to close everything, but "
                            f"the bot is OFF and {open_now} position(s) are "
                            "still open — it only sells while running. Press "
                            "<b>Start bot</b>; it will close them within "
                            "seconds. Press <b>Resume trading</b> only after "
                            "Open positions reaches 0.")
                return ("<b style='color:#b7791f'>Closing everything…</b> "
                        f"{open_now} position(s) still open — they close "
                        "within a few seconds. When Open positions reaches 0, "
                        "press the green <b>Resume trading</b> button.")
            if "flatten" in body:
                return ("<b style='color:#1f9d55'>All positions closed.</b> "
                        "Press the green <b>Resume trading</b> button to let "
                        "it trade again.")
            return ("<b style='color:#b7791f'>Trading is stopped by you.</b> "
                    "The bot is still watching and learning, but will not place "
                    "orders. Press the green <b>Resume trading</b> button above "
                    "to let it trade again. (Want a clean slate instead? "
                    "<b>Reset account</b> starts the simulation over at $100.)")
        if not running:
            failure = self.bot.start_error()
            if failure:
                return ("<b style='color:#c53030'>The bot could not start.</b> "
                        "It reported:<br><code>" + failure + "</code><br>"
                        "Fix the cause above (usually the settings file), then "
                        "press <b>Start bot</b> again.")
            return ("<b>The bot is not running.</b> Press <b>Start bot</b> "
                    "above. It only learns while it is on.")
        # Eight zeros with no explanation is the worst thing this panel can
        # show. If the bridge cannot see any markets, say so and say what to do
        # — every other number on this screen is downstream of it.
        if o["markets"] == 0:
            return ("<b style='color:#c53030'>Cannot see any markets.</b> The "
                    "bot is running but cannot reach Polymarket's market data, "
                    "so it cannot trade, learn or rank anyone — which is why "
                    "the numbers above are empty.<br><br>"
                    "Check your internet connection, and any firewall or VPN "
                    "that might be blocking it. It retries every cycle and will "
                    "pick up on its own as soon as the connection returns.")
        rows = o["research_rows"]
        if rows < 200:
            return (f"<b>Learning.</b> It studies the historical trade tapes of "
                    "settled markets (run backfill for more of those), and has "
                    f"collected <b>{rows:,}</b> live snapshots so far — live "
                    "series need roughly 200 per market, about <b>3–4 hours</b>. "
                    "Until rules survive validation it trades on market quality "
                    "and what the ranked traders are doing. Leave it running.")
        return ("<b>Collecting well.</b> There is now enough history that the "
                "Quant Bridge searches for trading rules on its own — see the "
                "<b>Discovery</b> tab for what it has found and validated. "
                "Wallet rankings keep improving as markets settle over the next "
                "few days.")

    def _fill_health(self):
        while self.health_grid.count():
            item = self.health_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for row, (what, value, colour) in enumerate(self.reader.health()):
            name = QLabel(what)
            name.setStyleSheet(f"color:{MUTED};")
            val = QLabel(value)
            val.setStyleSheet(f"color:{colour}; font-weight:bold;")
            val.setWordWrap(True)
            self.health_grid.addWidget(name, row, 0)
            self.health_grid.addWidget(val, row, 1)
        self.health_grid.setColumnStretch(1, 1)

    def _fill_previews(self):
        top = self.reader.wallets(6)
        self.preview_wallets.body.setText(
            "<br>".join(
                f"<b>{w['rank']}.</b> {w['label'] or w['wallet'][:12]} "
                f"<span style='color:{MUTED}'>score {w['score']:.2f}, "
                f"{w['sample']} trades</span>" for w in top)
            or "Nobody ranked yet — it needs settled markets to score anyone.")

        import time as _t
        recent = self.reader.anomalies(6)
        self.preview_anoms.body.setText(
            "<br>".join(
                f"<b>{a['kind'].replace('_',' ')}</b> "
                f"<span style='color:{MUTED}'>"
                f"{(a['label'] or a['subject'] or '')[:16]} · "
                f"{_t.strftime('%H:%M', _t.localtime(a['ts']))}</span>"
                for a in recent)
            or "Nothing unusual spotted yet.")

        acts = self.reader.decisions(6)
        self.preview_acts.body.setText(
            "<br>".join(
                f"<span style='color:{MUTED}'>"
                f"{_t.strftime('%H:%M:%S', _t.localtime(d['ts']))}</span>  "
                f"<b>{d['action']}</b>  {(d['outcome'] or '')[:22]}  "
                f"<span style='color:{MUTED}'>{d['reason'][:70]}</span>"
                for d in acts)
            or "No decisions yet.")

    def _fill_results(self):
        perf = self.reader.performance()
        rows, colours = [], {}
        # Friendlier names than the journal's column names, which are written
        # for the engine rather than for a person.
        labels = {"exit_style": "Why it exited", "category": "Market type",
                  "liquidity_bucket": "How liquid", "ttr_bucket": "Time left"}
        i = 0
        for dim, stats in (perf.get("groups") or {}).items():
            for s in stats:
                if s.n < 2:
                    continue
                rows.append([labels.get(dim, dim), s.key.replace("_", " "),
                             s.n, f"{s.win_rate:.0%}",
                             f"{s.mean_return:+.1%}", _money(s.total_pnl)])
                colours[i] = GREEN if s.total_pnl > 0 else RED
                i += 1
        self.results_table.fill(rows, colours)

        if not perf.get("sample"):
            self.results_note.setText(
                "<b>Nothing has finished yet.</b> This page fills in as trades "
                "close. It shows which kinds of trade have actually made money "
                "— by why they exited, what sort of market, how liquid it was, "
                "and how long was left before the market settled.")
            # The diagnostics pane still fills: it has its own "nothing to
            # diagnose yet" message, and a blank panel reads as a broken one.
            self._fill_money_management()
            self._fill_consistency()
            return

        bw = self.reader.best_and_worst()
        extra = ""
        if bw:
            extra = (f"<br>Best: <b>{bw['best']['outcome']}</b> "
                     f"{_money(bw['best']['realized_pnl'])} · "
                     f"Worst: <b>{bw['worst']['outcome']}</b> "
                     f"{_money(bw['worst']['realized_pnl'])}")
        bias = perf.get("bias") or {}
        if bias.get("applied"):
            better = "overriding" if bias.get("delta", 0) > 0 else "following"
            extra += (f"<br>When a tracked trader exits, <b>{better}</b> them has "
                      "worked out better so far — the bot is adjusting to that.")
        note = (f"<b>{perf['sample']} finished trades</b>, averaging "
                f"{perf['mean_return']:+.1%}. Groups with only one trade are "
                "hidden — one result is not evidence." + extra)
        if not perf.get("active"):
            note += ("<br><br><i>Not yet enough history for the bot to learn "
                     "from these; it needs about 20 finished trades.</i>")
        self.results_note.setText(note)
        self._fill_money_management()
        self._fill_consistency()

    def _fill_consistency(self):
        """§26: the CONSISTENCY ENGINE block. Compact on purpose.

        Three questions and no more: what does the return distribution look
        like, what is the health of what we are holding right now, and is the
        safety layer measuring or acting. The win rate is shown but not led
        with — it is the number that most tempts a reader into the wrong
        conclusion, and the expectancy beside it is the one that matters.
        """
        data = self.reader.consistency()
        census = self.reader.thesis_census()
        cfg = getattr(self.reader.cfg.engine, "consistency", None)
        mode = str(getattr(cfg, "mode", "shadow")).lower()

        candidates = (data.get("candidates") or {}) if data.get("available") \
            else {}
        promotable = candidates.get("promotable") or []
        if mode == "off":
            model = "BASELINE — the safety layer is switched off"
        elif mode == "enforce":
            model = "VALIDATED — the safety layer is acting on its findings"
        elif promotable:
            model = ("SHADOW — measuring only. "
                     f"{len(promotable)} candidate(s) have met the promotion "
                     "bar and are waiting for you to enable them")
        else:
            model = ("SHADOW — measuring only. No candidate has met the "
                     "promotion bar, so nothing is acting on anything")

        guard = "NORMAL"
        if census.get("INVALIDATED"):
            guard = f"ACTIVE — {census['INVALIDATED']} position(s) invalidated"
        elif census.get("WEAKENING"):
            guard = f"WATCHING — {census['WEAKENING']} position(s) weakening"

        parts = ["<b>CONSISTENCY ENGINE</b> — the shape of the returns, not "
                 "just the total."]
        if not data.get("available"):
            parts.append(
                "<i>Nothing to measure yet: "
                f"{data.get('reason', 'no closed trades')}.</i>")
        else:
            base = data["baseline"]
            growth = data.get("protectedGrowth") or {}
            parts.append(
                f"<b>Expectancy</b> {base['expectancy']:+.4f} per trade · "
                f"<b>average winner</b> {base['avgWinner']:+.2f} · "
                f"<b>average loser</b> {base['avgLoser']:+.2f} · "
                f"<b>win rate</b> {base['winRate']:.0%} · "
                f"<b>profit factor</b> {base['profitFactor']:.2f}")
            parts.append(
                f"<b>Max drawdown</b> ${base['maxDrawdown']:.2f} · "
                f"<b>95% loss</b> ${base['p95Loss']:.2f} · "
                f"<b>largest loss</b> ${abs(base['largestLoser']):.2f} · "
                f"<b>kept from peak</b> "
                f"{growth.get('retainedFromPeak', 0):.0%}")
            room = data.get("winnerRoom") or {}
            if room.get("available"):
                parts.append(
                    "<b>Room a winner needs:</b> nine winners in ten stay "
                    f"inside {room['winners']['p90']:.1%} of adverse "
                    "movement. Nothing may act inside that.")

        parts.append(
            f"<b>Thesis health (open):</b> healthy {census.get('HEALTHY', 0)} "
            f"· weakening {census.get('WEAKENING', 0)} · invalidated "
            f"{census.get('INVALIDATED', 0)} · unknown "
            f"{census.get('UNKNOWN', 0)}")
        parts.append(f"<b>Risk guard:</b> {guard} &nbsp;·&nbsp; "
                     f"<b>Exit model:</b> {model}")
        self.consistency_note.setText("<br>".join(parts))

    def _fill_money_management(self):
        """§23: what is hurting the equity curve, and what is saving it."""
        data = self.reader.money_management()
        if not data.get("available"):
            self.mm_note.setText(
                "<b>Money-management diagnostics</b> — nothing to diagnose "
                f"yet: {data.get('reason', 'no closed trades')}.")
            self.mm_table.fill([])
            return

        account = data["account"]
        attribution = data["exitAttribution"]
        costs = data.get("costs") or {}
        capture = data.get("upsideCapture") or {}
        sizing = data.get("sizing") or {}
        clusters = data.get("lossClusters") or {}
        correlated = data.get("correlatedExposure") or {}

        parts = [
            "<b>MONEY MANAGEMENT DIAGNOSTICS — why the account is where it "
            "is.</b> Everything here is a measurement of what already "
            "happened. Nothing on this page has been applied and nothing on "
            "it can change a stop, an exit, a position size or a strategy: "
            "proposals become new risk-policy versions and have to be tested "
            "on data they were not derived from before they may run.",
            f"<b>Account:</b> ${account['currentEquity']:.2f} of "
            f"${account['startingEquity']:.2f} · {account['closedTrades']} "
            f"closed, {account['openTrades']} open · realised "
            f"{account['realisedPnl']:+.2f} · win rate "
            f"{account['winRate']:.0%} ({account['winners']}W/"
            f"{account['losers']}L) · expectancy "
            f"{account['expectancy']:+.4f}/trade · average return "
            f"{account['avgReturn']:+.1%} · biggest winner "
            f"{account['largestWinner']:+.2f}, biggest loser "
            f"{account['largestLoser']:+.2f} · max drawdown "
            f"${account['maxDrawdown']:.2f}, current "
            f"${account['currentDrawdown']:.2f}.",
            f"<b>Gross vs net:</b> {costs.get('grossPnl')} before our own "
            f"costs, {costs.get('netPnl')} after · fees "
            f"{costs.get('fees')} · slippage {costs.get('slippage')}. "
            + str(costs.get("classification", "")),
        ]
        if capture.get("available"):
            parts.append(
                "<b>Protecting the winners:</b> upside capture "
                f"{capture['upsideCaptureRatio']:.2f} — of the favourable "
                "movement these positions actually showed, that is the share "
                "we banked. The top five winners are "
                f"{capture['topFiveWinnerShareOfProfit']:.0%} of all profit, "
                "so any change that shrinks them is not an improvement "
                "however much it reduces the drawdown.")
        parts.append("<b>Sizing:</b> " + str(sizing.get(
            "reading", sizing.get("reason", ""))))
        parts.append("<b>Loss shape:</b> " + str(clusters.get("verdict", ""))
                     + " " + str(correlated.get("reading", "")))

        hurting = attribution.get("destroyingMostValue") or "—"
        helping = attribution.get("preservingMostValue") or "—"
        parts.append(
            f"<b>Hurting the equity curve most:</b> exits by <b>{hurting}</b>."
            f" <b>Saving it most:</b> exits by <b>{helping}</b>. The table "
            "below breaks that down, then ranks the individual markets, "
            "categories and holding periods behind it.")

        proposals = data.get("hypotheses") or []
        changes = [h for h in proposals if h.get("status") == "PROPOSED"]
        if not changes:
            parts.append(
                "<b>Recommended changes: none.</b> Either the evidence does "
                "not support one or the sample is too small to support one. "
                "That is a valid result — the alternative is fitting a rule "
                "to the only trades that exist.")
        else:
            parts.append("<b>Research hypotheses raised (NOT applied):</b> "
                         + "; ".join(
                             f"{h['title']} (n={h['sample']})"
                             for h in changes))
        self.mm_note.setText("<br><br>".join(parts))

        rows, colours = [], {}
        counter = ((data.get("counterfactual") or {}).get("byExitStyle")
                   or {})
        i = 0
        for style, stats in sorted(
                attribution["byExitStyle"].items(),
                key=lambda kv: kv[1].get("netPnl", 0.0)):
            rows.append([
                "Why it exited", style.replace("_", " "), stats["trades"],
                _money(stats["netPnl"]),
                f"{stats.get('shareOfTotalLoss', 0):.0%}",
                f"{stats.get('shareOfTotalProfit', 0):.0%}",
                _hold_label(stats.get("medianHoldSeconds")),
                (counter.get(style) or {}).get("reading", "")[:120]])
            colours[i] = GREEN if stats["netPnl"] > 0 else RED
            i += 1
        for dimension, block in (data.get("contributors") or {}).items():
            for entry in block.get("hurting", []):
                rows.append([block["dimension"], str(entry["key"])[:32], "",
                             _money(entry["pnl"]), "", "", "",
                             "among the largest negative contributors"])
                colours[i] = RED
                i += 1
            for entry in block.get("helping", []):
                rows.append([block["dimension"], str(entry["key"])[:32], "",
                             _money(entry["pnl"]), "", "", "",
                             "among the largest positive contributors"])
                colours[i] = GREEN
                i += 1
        for bucket, stats in (data.get("byHoldingPeriod") or {}).items():
            rows.append([
                "How long it was held", bucket, stats["trades"],
                _money(stats["netPnl"]), "", "",
                _hold_label(stats.get("medianHoldSeconds")),
                ("" if stats["claimable"]
                 else "too few trades to conclude anything")])
            colours[i] = GREEN if stats["netPnl"] > 0 else RED
            i += 1
        self.mm_table.fill(rows, colours)

    def _fill_tables(self):
        wallets = self.reader.wallets()
        rows_wall = []
        for w in wallets:
            r = w.get("research") or {}
            two = r.get("twoSided") or {}
            kinds = two.get("kinds") or {}
            hedge_like = int(kinds.get("simultaneous_two_sided") or 0)
            reversal = int(kinds.get("sequential_two_sided") or 0)
            incremental = two.get("matchedIncremental")
            rows_wall.append([
                w["rank"], w["label"] or w["wallet"][:14], f"{w['score']:.3f}",
                f"{w['confidence']:.0%}", w["sample"], f"{w['win_rate']:.0%}",
                f"{w['avg_return']:+.1%}", w["markets"],
                # Shown because it changes what a trader's buy MEANS: a
                # hedger holds both outcomes, so its buy is one leg of a
                # near-riskless pair, not a view on who wins.
                f"{w.get('hedge_rate', 0):.0%}" if w.get("hedge_rate") else "",
                # -- research overlay: behaviour, not verdicts ------------
                str(r.get("independentMarkets") or ""),
                (f"{hedge_like}/{reversal}" if (hedge_like or reversal)
                 else ("0" if r else "")),
                (f"{two['twoSided']['expectancy']:+.3f}"
                 if two.get("twoSided", {}).get("markets") else ""),
                (f"{two['oneSided']['expectancy']:+.3f}"
                 if two.get("oneSided", {}).get("markets") else ""),
                (f"{incremental:+.3f}" if incremental is not None else ""),
                _hold_label(r.get("medianHold")),
                str(r.get("entryPriceBias") or ""),
                (f"{r['sampleQuality']:.2f}" if r.get("sampleQuality")
                 else ""),
                (f"{r['researchPriority']:.2f}" if r.get("researchPriority")
                 else ""),
                "yes" if w["in_cohort"] else ""])
        self._tbl_wall.fill(rows_wall)
        pooled = (self.reader.wallet_research().get("twoSided") or {})
        note = ("Traders the bot found on its own and scored on how their "
                "past trades actually turned out. Nothing here was typed in "
                "by hand. <b>Score is sample-size aware</b>: a wallet's own "
                "record is pulled toward the population average until it has "
                "roughly 25 scored trades behind it, so a spectacular "
                "8-trade run cannot outrank a long consistent record, and "
                "single-trade returns are capped before averaging.")
        if pooled.get("verdict"):
            note += ("<br><br><b>Does playing both sides actually help?</b> "
                     + str(pooled["verdict"])
                     + " <i>Hedge/reversal counts markets where the trader "
                     "held both outcomes at once versus exited one side "
                     "before taking the other — different behaviours, never "
                     "summed. A share of two-sided activity is an "
                     "observation; only independent out-of-sample testing "
                     "can turn it into a proven edge.</i>")
        self._note_wall.setText(note)

        import time as _t
        rows, colours = [], {}
        for i, a in enumerate(self.reader.anomalies(150)):
            rows.append([
                _t.strftime("%d %b %H:%M", _t.localtime(a["ts"])),
                a["kind"].replace("_", " "),
                (a["label"] or a["subject"] or "")[:26],
                f"{a['z']:.1f}x normal", f"{a['strength']:.0%}"])
            if a["strength"] > 0.8:
                colours[i] = AMBER
        self._tbl_unus.fill(rows, colours)
        summary = ", ".join(f"{r['kind'].replace('_',' ')} {r['n']}"
                            for r in self.reader.anomaly_summary())
        self._note_unus.setText(
            "Behaviour that is unusual for that particular trader or market — "
            "not unusual in general. " + (f"So far: {summary}." if summary else ""))

        positions = self.reader.positions()
        equity = float(self.reader.overview().get("equity") or 0.0)
        # Correlated exposure, from the same rule the risk policy uses: two
        # positions sharing a market are one bet held twice, and a column that
        # showed only "2 open" would hide that.
        by_market: dict[str, int] = {}
        for p in positions:
            key = str(p.get("market_id") or "")
            by_market[key] = by_market.get(key, 0) + 1
        rows_open, colours_open = [], {}
        for i, p in enumerate(positions):
            entry = float(p.get("entry_price") or 0.0)
            size = float(p.get("entry_size") or 0.0)
            cost = float(p.get("entry_cost") or 0.0)
            peak = float(p.get("peak_price") or 0.0)
            trough = float(p.get("trough_price") or 0.0)
            # No live mark is available to a read-only dashboard, so the most
            # recent extreme is used and labelled as such rather than a stale
            # "current price" that is really the entry.
            mark = peak if peak > 0 else entry
            unrealised = (mark - entry) * size if entry else 0.0
            siblings = by_market.get(str(p.get("market_id") or ""), 1) - 1
            held = (_t.time() - float(p.get("entry_ts") or 0.0)
                    if p.get("entry_ts") else 0)
            rows_open.append([
                p["outcome"], (p["question"] or "")[:40], f"{size:.2f}",
                f"${entry:.2f}", _money(cost), f"${mark:.2f}",
                _money(unrealised),
                f"{cost / equity:.0%}" if equity > 0 else "-",
                (f"{siblings} other position(s) in this market"
                 if siblings else "nothing else open here"),
                _hold_label(held),
                f"${trough:.2f}" if trough else "-",
                f"${peak:.2f}" if peak else "-"])
            colours_open[i] = GREEN if unrealised > 0 else RED
        self._tbl_open.fill(rows_open, colours_open)
        self._note_open.setText(
            "Trades the bot currently has money in, with what each is costing "
            "the account in risk. <b>\"Correlated with\"</b> is the one that "
            "matters for money management: positions sharing a market are one "
            "bet held twice, not two independent risks — the capital-"
            "preservation caps count them that way too. Nothing here forces "
            "an exit: the validated strategy and the existing exit ladder stay "
            "in charge of when a position closes.")

        rows, colours = [], {}
        counterfactual = self.reader.counterfactual_by_trade()
        for i, t in enumerate(self.reader.trades(150)):
            delta = counterfactual.get(int(t.get("id") or 0))
            cost = float(t.get("entry_cost") or 0.0)
            rows.append([
                t["outcome"], f"${t['entry_price']:.2f}",
                f"${t['exit_price']:.2f}", _money(t["realized_pnl"]),
                f"{(t['return_pct'] or 0)*100:+.1f}%",
                (t["exit_style"] or "").replace("_", " "),
                _hold_label(t.get("hold_seconds")),
                f"{cost / equity:.0%}" if equity > 0 and cost else "-",
                # Polymarket publishes no per-fill fee today, so this is
                # blank rather than "$0.00" — an unreported cost and a zero
                # cost are different facts.
                "-",
                (_money(delta) if delta is not None else "no data")])
            colours[i] = GREEN if (t["realized_pnl"] or 0) > 0 else RED
        self._tbl_clos.fill(rows, colours)
        self._note_clos.setText(
            "Finished trades and what each one made or lost, after fees. "
            "<b>\"If held 30m longer\"</b> is a COUNTERFACTUAL: what the same "
            "position would have been worth at the price captured half an "
            "hour after we actually got out. It is research only — it is "
            "never added to the result, never counted as evidence, and no "
            "rule is changed because of it. A column of large positive "
            "numbers is a question worth investigating, not a proof that the "
            "exits are wrong.")

        lines = []
        for d in self.reader.decisions():
            when = _t.strftime("%H:%M:%S", _t.localtime(d["ts"]))
            who = f"  [{d['wallet_influence']}]" if d["wallet_influence"] else ""
            lines.append(f"{when}  {d['action']:<11} {(d['outcome'] or '')[:24]:<24} "
                         f"{d['reason']}{who}")
        self.activity.setPlainText("\n".join(lines) or "No decisions yet.")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    # Explicit dark theme: `--dark` or PQB_THEME=dark. Light stays the
    # default so existing installs look exactly as they always have.
    dark = "--dark" in argv or \
        os.environ.get("PQB_THEME", "").lower() == "dark"
    argv = [a for a in argv if a != "--dark"]
    root = Path(__file__).resolve().parents[2]
    config_path = Path(argv[1]) if len(argv) > 1 else root / "config" / "config.yaml"

    from ..config import load
    if not config_path.exists():
        example = config_path.parent / "config.example.yaml"
        if example.exists():
            config_path.write_text(example.read_text(encoding="utf-8"),
                                   encoding="utf-8")

    app = QApplication(argv)
    app.setApplicationName("Polymarket Quant Bridge")
    apply_theme(app, dark)
    try:
        config = load(config_path)
    except Exception as exc:                            # noqa: BLE001
        QMessageBox.critical(None, "Cannot start",
                             f"Could not read the settings file:\n\n{exc}")
        return 1

    window = Dashboard(config, config_path)
    # The build stamp in the TITLE: "which version are you running?" must
    # be answerable from any screenshot, because every "nothing changed"
    # report turns on exactly that question.
    window.setWindowTitle(
        f"Polymarket Quant Bridge — build {config.build_version()}")
    # The Extract-All trap, called out loudly instead of silently running
    # old code: an update extracted INTO the install nests a fresh copy
    # one level down while the operator keeps launching the old one.
    nested = config.nested_install()
    if nested is not None:
        QMessageBox.warning(
            None, "Update did not install correctly",
            "A newer copy of the bot is nested INSIDE this folder:\n\n"
            f"{nested}\n\n"
            "This happens when the update zip is extracted INTO the bot "
            "folder instead of over it. You are currently running the OLD "
            "version.\n\nFix: close this window, move the contents of "
            "that nested folder up one level (replacing files), delete "
            "the empty nested folder, and start again.")
    window.show()
    return app.exec()
