"""Structured logging for the autonomous platform.

Every module logs through here so that OpenClaw, the GUI, the REST/WS API and
the on-disk log all see the SAME event stream in the same shape.

Records are emitted as one JSON object per line (newline-delimited JSON), which
is trivially greppable and replayable:

    {"ts": "2026-07-14T09:31:02.481", "level": "INFO", "module": "ingest",
     "msg": "engine warm", "ticks": 120000, "rss_mb": 88.4}

A human-readable console/GUI formatter is used for the same records so the user
never has to read raw JSON.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

_CONFIGURED = False
_SUBSCRIBERS: list[Callable[[dict], None]] = []

# Fields the stdlib puts on every LogRecord; anything else the caller attached
# via `extra=` is treated as structured context and included in the JSON.
_STD_FIELDS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


def _context(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _STD_FIELDS}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "module": record.name,
            "msg": record.getMessage(),
        }
        payload.update(_context(record))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    """Compact one-liner for the console and the GUI log pane."""

    def format(self, record: logging.LogRecord) -> str:
        t = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        ctx = _context(record)
        tail = " ".join(f"{k}={v}" for k, v in ctx.items()) if ctx else ""
        line = f"{t} {record.levelname[:4]:<4} [{record.name}] {record.getMessage()}"
        return f"{line}  {tail}".rstrip()


class FanoutHandler(logging.Handler):
    """Pushes every record to live subscribers (GUI pane, WebSocket clients).

    Subscribers must never raise and must never block; a failing subscriber is
    dropped rather than being allowed to take the logging system down.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if not _SUBSCRIBERS:
            return
        payload = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "module": record.name,
            "msg": record.getMessage(),
            **_context(record),
        }
        for fn in list(_SUBSCRIBERS):
            try:
                fn(payload)
            except Exception:  # noqa: BLE001
                _SUBSCRIBERS.remove(fn)


def subscribe(fn: Callable[[dict], None]) -> Callable[[], None]:
    """Register a live log sink. Returns an unsubscribe callable."""
    _SUBSCRIBERS.append(fn)

    def _off() -> None:
        if fn in _SUBSCRIBERS:
            _SUBSCRIBERS.remove(fn)

    return _off


def setup(log_dir: str | os.PathLike | None = None, level: str = "INFO",
          console: bool = True, json_file: bool = True) -> Path | None:
    """Configure root logging once. Safe to call repeatedly."""
    global _CONFIGURED
    if _CONFIGURED:
        return None

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    path: Path | None = None
    if json_file and log_dir:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "platform.jsonl"
        # Rotate so an unattended 24/7 run can never fill the disk.
        fh = logging.handlers.RotatingFileHandler(
            path, maxBytes=8_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)

    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(HumanFormatter())
        root.addHandler(sh)

    root.addHandler(FanoutHandler())

    # Third-party chatter stays out of the research log.
    for noisy in ("ib_insync", "ib_insync.wrapper", "ib_insync.client", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    return path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def rss_mb() -> float:
    """Resident memory in MB - used by the low-RAM guardrails. 0.0 if unknown."""
    try:  # psutil is optional
        import psutil  # type: ignore
        return psutil.Process().memory_info().rss / 1e6
    except Exception:  # noqa: BLE001
        pass
    if os.name == "nt":
        try:
            import ctypes
            import ctypes.wintypes as wt

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            k32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            # On 64-bit, GetCurrentProcess returns a HANDLE. ctypes defaults the
            # restype to c_int and TRUNCATES it, so the call silently fails
            # (returns 0) unless the types are declared explicitly.
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_PMC), wt.DWORD]
            psapi.GetProcessMemoryInfo.restype = wt.BOOL

            pmc = _PMC()
            pmc.cb = ctypes.sizeof(_PMC)
            if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(),
                                          ctypes.byref(pmc), pmc.cb):
                return pmc.WorkingSetSize / 1e6
        except Exception:  # noqa: BLE001
            pass
    return 0.0
