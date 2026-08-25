"""Collector infrastructure.

Every collector reports health on every run, including when it does nothing.
A collector that is silent when idle is indistinguishable from one that has
crashed, and the SYSTEM tab exists precisely to tell those apart.

Networking is stdlib `urllib` with an explicit timeout and bounded concurrency.
No `requests`, no `aiohttp` — V2 established that this project ships with zero
dependencies and nothing here justifies breaking that.

Nothing dials out unless `collectors.enabled` is True. A research tool that
makes network calls the moment it is imported is a tool people stop running.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..config import Settings
from ..secrets import redact

USER_AGENT = "polymarket-quant-bridge-v3/1.0 (local research)"


@dataclass
class CollectorRun:
    collector: str
    status: str = "OK"                 # OK|STALE|ERROR|NOT_CONFIGURED|DISABLED
    rows: int = 0
    error: str = ""
    detail: str = ""
    elapsed_ms: int = 0
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "OK"

    def to_dict(self) -> dict:
        return {"collector": self.collector, "status": self.status,
                "rows": self.rows, "error": self.error, "detail": self.detail,
                "elapsed_ms": self.elapsed_ms, "notes": self.notes}


def http_json(url: str, *, timeout: float = 10.0, params: dict | None = None):
    """GET and parse JSON. Returns (data, error_string).

    Never raises. A collector that raises on a transient 502 stops collecting;
    one that records the error keeps running and tells you what happened.
    """
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace")), ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"URL error: {redact(str(e.reason))}"
    except json.JSONDecodeError:
        return None, "response was not JSON"
    except Exception as e:                                    # noqa: BLE001
        return None, f"{type(e).__name__}: {redact(str(e))}"


def http_text(url: str, *, timeout: float = 10.0):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace"), ""
    except Exception as e:                                    # noqa: BLE001
        return None, f"{type(e).__name__}: {redact(str(e))}"


class Collector:
    """Base class. Subclasses implement `_run` and declare `name`."""

    name = "collector"
    requires_config = ()               # attribute names on CollectorConfig

    def __init__(self, st: Settings, store) -> None:
        self.st = st
        self.store = store

    # -- to override --------------------------------------------------------
    def _run(self, run: CollectorRun) -> None:
        raise NotImplementedError

    # -- driver -------------------------------------------------------------
    def run(self) -> CollectorRun:
        t0 = time.perf_counter()
        run = CollectorRun(collector=self.name)

        if not self.st.collectors.enabled:
            run.status = "DISABLED"
            run.detail = ("collectors are off; enable with "
                          "`pqv3 collect --enable`")
            self._record(run)
            return run

        missing = [c for c in self.requires_config
                   if not getattr(self.st.collectors, c, None)]
        if missing:
            run.status = "NOT_CONFIGURED"
            run.detail = f"unset: {', '.join(missing)}"
            self._record(run)
            return run

        try:
            self._run(run)
        except Exception as e:                                # noqa: BLE001
            run.status = "ERROR"
            run.error = redact(f"{type(e).__name__}: {e}")

        run.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        self._record(run)
        return run

    def _record(self, run: CollectorRun) -> None:
        self.store.record_health(self.name, run.status, error=run.error,
                                 rows_total=run.rows,
                                 detail=run.detail or "; ".join(run.notes),
                                 success=run.ok and run.rows >= 0)

    # -- helpers ------------------------------------------------------------
    def fetch_many(self, urls: list, *, timeout: float | None = None) -> list:
        """Bounded-concurrency GET. Order preserved."""
        t = timeout or self.st.collectors.http_timeout_secs
        with ThreadPoolExecutor(
                max_workers=self.st.collectors.max_inflight) as pool:
            return list(pool.map(lambda u: http_json(u, timeout=t), urls))
