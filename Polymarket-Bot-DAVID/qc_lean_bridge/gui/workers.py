"""Background worker threads.

Every phase runs off the GUI thread so the window stays responsive. The worker
receives a closure that takes (log_cb, progress_cb); those callbacks are wired to
Qt signals which are safe to emit across threads.
"""
from __future__ import annotations

import traceback

from .qt import QThread, pyqtSignal


class Worker(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)          # done, total
    done = pyqtSignal(str, object)           # phase name, result
    failed = pyqtSignal(str, str)            # phase name, error text

    def __init__(self, name: str, fn, parent=None):
        super().__init__(parent)
        self.name = name
        self._fn = fn

    def _log(self, msg: str):
        self.log.emit(str(msg))

    def _progress(self, done: int, total: int):
        self.progress.emit(int(done), int(total))

    def run(self):
        try:
            result = self._fn(self._log, self._progress)
            self.done.emit(self.name, result)
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            self.failed.emit(self.name, f"{exc}\n\n{tb}")
