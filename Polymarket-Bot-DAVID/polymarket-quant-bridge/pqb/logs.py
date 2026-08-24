"""
Structured logging.

Same shape as the upstream project's ``applog`` — a rotating file handler plus a
stream handler, one named logger, configured once — but writing to this
project's own data directory instead of the other project's, and exposing a
``bind()`` helper so every line carries the cycle it belongs to.

``event()`` emits one key=value line per record. Grepping a session for
``event=decision`` or ``event=reconcile.mismatch`` is the intended way to read a
long dry-run, and it is trivially parseable if it ever needs to feed a dashboard.

Nothing here formats a credential: values are stringified as given, and the only
config representation any caller is handed is ``Config.redacted()``.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

LOGGER_NAME = "pqb"

_SECRET_HINTS = ("private_key", "secret", "passphrase", "api_key")


def setup(level: str = "INFO", file: Optional[Path] = None,
          max_bytes: int = 2_000_000, backups: int = 5) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    if file is not None:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(file, maxBytes=max_bytes, backupCount=backups,
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def _safe(key: str, value: Any) -> str:
    lowered = key.lower()
    if any(hint in lowered for hint in _SECRET_HINTS):
        return "***"
    if isinstance(value, float):
        return f"{value:.6g}"
    text = str(value)
    if " " in text or "=" in text:
        return '"' + text.replace('"', "'") + '"'
    return text


class Log:
    """A logger with a fixed prefix of context fields."""

    def __init__(self, logger: Optional[logging.Logger] = None,
                 **context: Any):
        self._logger = logger or logging.getLogger(LOGGER_NAME)
        self._context = context

    def bind(self, **context: Any) -> "Log":
        merged = dict(self._context)
        merged.update(context)
        return Log(self._logger, **merged)

    def _render(self, message: str, fields: dict) -> str:
        parts = [message]
        for key, value in {**self._context, **fields}.items():
            if value is None:
                continue
            parts.append(f"{key}={_safe(key, value)}")
        return " ".join(parts)

    def debug(self, message: str, **fields: Any) -> None:
        self._logger.debug(self._render(message, fields))

    def info(self, message: str, **fields: Any) -> None:
        self._logger.info(self._render(message, fields))

    def warning(self, message: str, **fields: Any) -> None:
        self._logger.warning(self._render(message, fields))

    def error(self, message: str, **fields: Any) -> None:
        self._logger.error(self._render(message, fields))

    def event(self, name: str, **fields: Any) -> None:
        """A machine-greppable record: ``event=<name>`` plus its fields."""
        self.info(f"event={name}", **fields)
