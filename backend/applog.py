"""
Structured application logging.

Every message goes three places, exactly as the desktop build did:
  1. A rotating log file in the app data directory (post-mortem debugging).
  2. The SQLite ``activity_log`` table (history survives restarts).
  3. The broadcast hub, so every open browser tab sees the line immediately.

The hub hop is non-blocking, so logging from inside the trading loop costs
nothing measurable.
"""

from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from typing import Optional

from .db import Database
from .hub import Hub
from .paths import app_data_dir

LEVELS = ("INFO", "SUCCESS", "WARNING", "ERROR")


class AppLogger:
    def __init__(self, db: Optional[Database] = None, hub: Optional[Hub] = None):
        self.db = db
        self.hub = hub

        self._logger = logging.getLogger("polybot")
        self._logger.setLevel(logging.INFO)
        if not self._logger.handlers:
            fmt = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
            )
            fh = RotatingFileHandler(
                app_data_dir() / "bot.log",
                maxBytes=2_000_000, backupCount=5, encoding="utf-8",
            )
            fh.setFormatter(fmt)
            self._logger.addHandler(fh)
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            self._logger.addHandler(sh)

    def emit(self, level: str, message: str) -> None:
        ts = int(time.time())
        level = level.upper()
        # SUCCESS is not a stdlib level; log as INFO but tag it for the UI.
        std = "info" if level == "SUCCESS" else level.lower()
        getattr(self._logger, std, self._logger.info)(message)
        if self.db is not None:
            try:
                self.db.add_log(level, message, ts)
            except Exception:
                pass
        if self.hub is not None:
            self.hub.publish("log", {"level": level, "message": message, "ts": ts})

    def info(self, message: str) -> None:
        self.emit("INFO", message)

    def warning(self, message: str) -> None:
        self.emit("WARNING", message)

    def error(self, message: str) -> None:
        self.emit("ERROR", message)

    def success(self, message: str) -> None:
        self.emit("SUCCESS", message)
