"""Replayable event database.

Everything the engines see and produce is persisted so any research run can be
REPLAYED bit-for-bit later:

    ticks              raw IBKR Time & Sales (the ground truth)
    structural_events  non-print / void / skipped price / gap / void filled
    line_break_lines   lines printed by the resolution engines
    feature_snapshots  the research rows

Backends (chosen by `database.backend`):
    sqlite     zero-install, single file - the default, and what the exe ships
               with. WAL + batched inserts handle millions of ticks fine.
    postgres   PostgreSQL
    timescale  PostgreSQL + TimescaleDB hypertables on ts (best for tick scale)

`psycopg2` is an OPTIONAL dependency: with no Postgres on the machine the
platform silently uses SQLite, so nothing here can stop the app from running.

Writes are BATCHED (executemany every `batch_size` rows) - that is the single
biggest factor in keeping ingest CPU low and throughput high.
"""
from __future__ import annotations

import json
import os
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .events import Tick, StructuralEvent, LineBreakEvent, FeatureSnapshot
from .logging_setup import get_logger

log = get_logger("db")


def psycopg2_available() -> bool:
    try:
        import psycopg2  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


@dataclass
class DbConfig:
    backend: str = "sqlite"
    path: str = "data/research.db"          # sqlite
    host: str = "localhost"                 # postgres / timescale
    port: int = 5432
    database: str = "research"
    user: str = "postgres"
    password: str = ""
    batch_size: int = 5000
    store_ticks: bool = True

    @classmethod
    def from_config(cls, cfg) -> "DbConfig":
        c = cfg.section("database") if hasattr(cfg, "section") else dict(cfg)
        return cls(
            backend=str(c.get("backend", "sqlite")).lower(),
            path=str(c.get("path", "data/research.db")),
            host=str(c.get("host", "localhost")),
            port=int(c.get("port", 5432)),
            database=str(c.get("database", "research")),
            user=str(c.get("user", "postgres")),
            password=str(c.get("password", "")),
            batch_size=int(c.get("batch_size", 5000)),
            store_ticks=bool(c.get("store_ticks", True)),
        )


class EventStore(ABC):
    """Common surface for every backend."""

    # Commit after this many batches. Batches are one transaction each way:
    # too frequent and we pay fsync; never and the WAL grows without bound on a
    # multi-hour ingest. ~10 batches (=50k rows by default) is the balance.
    COMMIT_EVERY_BATCHES = 10

    def __init__(self, cfg: DbConfig):
        self.cfg = cfg
        self._ticks: list[tuple] = []
        self._structural: list[tuple] = []
        self._lines: list[tuple] = []
        self._features: list[tuple] = []
        self.counts = {"ticks": 0, "structural": 0, "lines": 0, "features": 0}
        self.run_id: int = 0
        self._batches = 0

    # ---- lifecycle -------------------------------------------------------
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def _executemany(self, sql: str, rows: Sequence[tuple]) -> None: ...

    @abstractmethod
    def _execute(self, sql: str, params: Sequence = ()) -> Any: ...

    @abstractmethod
    def _query(self, sql: str, params: Sequence = ()) -> list[tuple]: ...

    @abstractmethod
    def commit(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def ph(self) -> str:
        """Parameter placeholder: '?' for sqlite, '%s' for postgres."""

    # ---- schema ----------------------------------------------------------
    def create_schema(self) -> None:
        self._execute("""
            CREATE TABLE IF NOT EXISTS ticks (
                ts DOUBLE PRECISION NOT NULL,
                price DOUBLE PRECISION NOT NULL,
                size INTEGER,
                bid DOUBLE PRECISION,
                bid_size INTEGER,
                ask DOUBLE PRECISION,
                ask_size INTEGER
            )""")
        self._execute("""
            CREATE TABLE IF NOT EXISTS structural_events (
                ts DOUBLE PRECISION NOT NULL,
                side TEXT NOT NULL,
                kind TEXT NOT NULL,
                price DOUBLE PRECISION,
                size DOUBLE PRECISION,
                ticks INTEGER,
                persistence DOUBLE PRECISION
            )""")
        self._execute("""
            CREATE TABLE IF NOT EXISTS line_break_lines (
                ts DOUBLE PRECISION NOT NULL,
                resolution INTEGER NOT NULL,
                direction INTEGER,
                open DOUBLE PRECISION,
                close DOUBLE PRECISION,
                reversal INTEGER,
                run_length INTEGER,
                seconds DOUBLE PRECISION,
                ticks_consumed INTEGER
            )""")
        self._execute("""
            CREATE TABLE IF NOT EXISTS feature_snapshots (
                ts DOUBLE PRECISION NOT NULL,
                price DOUBLE PRECISION,
                payload TEXT NOT NULL
            )""")
        self._execute("""
            CREATE TABLE IF NOT EXISTS ingest_runs (
                run_id INTEGER PRIMARY KEY,
                started TEXT,
                finished TEXT,
                source TEXT,
                ticks INTEGER,
                structural INTEGER,
                lines INTEGER,
                features INTEGER,
                notes TEXT
            )""")
        for sql in (
            "CREATE INDEX IF NOT EXISTS ix_ticks_ts ON ticks(ts)",
            "CREATE INDEX IF NOT EXISTS ix_struct_ts ON structural_events(ts)",
            "CREATE INDEX IF NOT EXISTS ix_struct_kind ON structural_events(kind)",
            "CREATE INDEX IF NOT EXISTS ix_lines_ts ON line_break_lines(ts)",
            "CREATE INDEX IF NOT EXISTS ix_lines_res ON line_break_lines(resolution)",
            "CREATE INDEX IF NOT EXISTS ix_feat_ts ON feature_snapshots(ts)",
        ):
            try:
                self._execute(sql)
            except Exception as exc:  # noqa: BLE001
                log.debug("index skipped", extra={"sql": sql, "err": str(exc)})
        self.commit()

    # ---- writes (batched) ------------------------------------------------
    def add_tick(self, t: Tick) -> None:
        if not self.cfg.store_ticks:
            self.counts["ticks"] += 1
            return
        self._ticks.append(t.as_row())
        self.counts["ticks"] += 1
        if len(self._ticks) >= self.cfg.batch_size:
            self.flush_ticks()

    def add_structural(self, events: list[StructuralEvent]) -> None:
        if not events:
            return
        for e in events:
            self._structural.append(e.as_row())
        self.counts["structural"] += len(events)
        if len(self._structural) >= self.cfg.batch_size:
            self.flush_structural()

    def add_lines(self, events: list[LineBreakEvent]) -> None:
        if not events:
            return
        for e in events:
            self._lines.append(e.as_row())
        self.counts["lines"] += len(events)
        if len(self._lines) >= self.cfg.batch_size:
            self.flush_lines()

    def add_feature(self, snap: FeatureSnapshot) -> None:
        self._features.append((snap.ts, snap.price, json.dumps(
            {k: round(float(v), 6) for k, v in snap.values.items()})))
        self.counts["features"] += 1
        if len(self._features) >= max(200, self.cfg.batch_size // 10):
            self.flush_features()

    def _maybe_commit(self) -> None:
        self._batches += 1
        if self._batches >= self.COMMIT_EVERY_BATCHES:
            self._batches = 0
            self.commit()

    def flush_ticks(self) -> None:
        if self._ticks:
            self._executemany(
                f"INSERT INTO ticks (ts,price,size,bid,bid_size,ask,ask_size) "
                f"VALUES ({','.join([self.ph]*7)})", self._ticks)
            self._ticks.clear()
            self._maybe_commit()

    def flush_structural(self) -> None:
        if self._structural:
            self._executemany(
                f"INSERT INTO structural_events "
                f"(ts,side,kind,price,size,ticks,persistence) "
                f"VALUES ({','.join([self.ph]*7)})", self._structural)
            self._structural.clear()
            self._maybe_commit()

    def flush_lines(self) -> None:
        if self._lines:
            self._executemany(
                f"INSERT INTO line_break_lines "
                f"(ts,resolution,direction,open,close,reversal,run_length,"
                f"seconds,ticks_consumed) VALUES ({','.join([self.ph]*9)})",
                self._lines)
            self._lines.clear()
            self._maybe_commit()

    def flush_features(self) -> None:
        if self._features:
            self._executemany(
                f"INSERT INTO feature_snapshots (ts,price,payload) "
                f"VALUES ({','.join([self.ph]*3)})", self._features)
            self._features.clear()
            self._maybe_commit()

    def flush(self) -> None:
        self.flush_ticks()
        self.flush_structural()
        self.flush_lines()
        self.flush_features()
        self.commit()

    # ---- run bookkeeping -------------------------------------------------
    def start_run(self, source: str) -> int:
        row = self._query("SELECT COALESCE(MAX(run_id),0) FROM ingest_runs")
        self.run_id = int(row[0][0]) + 1 if row else 1
        self._execute(
            f"INSERT INTO ingest_runs (run_id,started,source,ticks,structural,"
            f"lines,features) VALUES ({','.join([self.ph]*7)})",
            (self.run_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             source, 0, 0, 0, 0))
        self.commit()
        return self.run_id

    def finish_run(self, notes: str = "") -> None:
        self._execute(
            f"UPDATE ingest_runs SET finished={self.ph}, ticks={self.ph}, "
            f"structural={self.ph}, lines={self.ph}, features={self.ph}, "
            f"notes={self.ph} WHERE run_id={self.ph}",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),
             self.counts["ticks"], self.counts["structural"],
             self.counts["lines"], self.counts["features"], notes, self.run_id))
        self.commit()

    # ---- replay ----------------------------------------------------------
    def replay_ticks(self, start: float | None = None, end: float | None = None,
                     chunk: int = 50_000) -> Iterator[Tick]:
        """Stream stored ticks back in time order - the replay guarantee.

        Chunked by ts so a multi-million-row replay never materializes in RAM.
        """
        where, params = [], []
        if start is not None:
            where.append(f"ts >= {self.ph}")
            params.append(start)
        if end is not None:
            where.append(f"ts <= {self.ph}")
            params.append(end)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        offset = 0
        while True:
            rows = self._query(
                f"SELECT ts,price,size,bid,bid_size,ask,ask_size FROM ticks"
                f"{clause} ORDER BY ts LIMIT {int(chunk)} OFFSET {int(offset)}",
                params)
            if not rows:
                return
            for r in rows:
                yield Tick(ts=float(r[0]), price=float(r[1]), size=int(r[2] or 0),
                           bid=float(r[3] or 0), bid_size=int(r[4] or 0),
                           ask=float(r[5] or 0), ask_size=int(r[6] or 0))
            offset += len(rows)
            if len(rows) < chunk:
                return

    def recent_structural(self, limit: int = 200) -> list[dict]:
        rows = self._query(
            f"SELECT ts,side,kind,price,size,ticks,persistence FROM "
            f"structural_events ORDER BY ts DESC LIMIT {int(limit)}")
        return [{"ts": r[0], "side": r[1], "kind": r[2], "price": r[3],
                 "size": r[4], "ticks": r[5], "persistence": r[6]} for r in rows]

    def feature_rows(self, limit: int = 0) -> list[dict]:
        sql = "SELECT ts,price,payload FROM feature_snapshots ORDER BY ts"
        if limit:
            sql += f" LIMIT {int(limit)}"
        out = []
        for ts, price, payload in self._query(sql):
            row = {"ts": ts, "price": price}
            row.update(json.loads(payload))
            out.append(row)
        return out

    def summary(self) -> dict:
        def one(sql: str) -> int:
            try:
                r = self._query(sql)
                return int(r[0][0] or 0) if r else 0
            except Exception:  # noqa: BLE001
                return 0
        return {
            "backend": self.cfg.backend,
            "ticks": one("SELECT COUNT(*) FROM ticks"),
            "structural_events": one("SELECT COUNT(*) FROM structural_events"),
            "line_break_lines": one("SELECT COUNT(*) FROM line_break_lines"),
            "feature_snapshots": one("SELECT COUNT(*) FROM feature_snapshots"),
            "runs": one("SELECT COUNT(*) FROM ingest_runs"),
        }


# ------------------------------------------------------------------- sqlite
class SqliteStore(EventStore):
    ph = "?"

    def __init__(self, cfg: DbConfig, root: Path):
        super().__init__(cfg)
        p = Path(cfg.path)
        self.path = p if p.is_absolute() else (root / p)
        self.conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # NOTE: do NOT pass isolation_level=None here. Autocommit makes sqlite3
        # wrap EVERY row of an executemany in its own transaction (one WAL commit
        # each), which measured ~0.5 s per 5,000-row batch - 78% of total ingest
        # time. With the default deferred isolation, a batch is ONE transaction
        # and the same insert costs ~10 ms.
        self.conn = sqlite3.connect(str(self.path))
        cur = self.conn.cursor()
        # WAL + relaxed sync: this is a research store being written at tick
        # rate, not a bank ledger. An OS crash costs the tail of one run, and
        # buys an order of magnitude of write throughput.
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-40000")     # ~40 MB page cache
        cur.close()
        self.create_schema()
        log.info("sqlite store ready", extra={"path": str(self.path)})

    def _executemany(self, sql: str, rows: Sequence[tuple]) -> None:
        self.conn.executemany(sql, rows)

    def _execute(self, sql: str, params: Sequence = ()) -> Any:
        return self.conn.execute(sql, params)

    def _query(self, sql: str, params: Sequence = ()) -> list[tuple]:
        cur = self.conn.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return rows

    def commit(self) -> None:
        if self.conn is not None:
            self.conn.commit()

    def close(self) -> None:
        if self.conn is not None:
            self.flush()
            self.conn.close()
            self.conn = None


# ---------------------------------------------------------------- postgres
class PostgresStore(EventStore):
    ph = "%s"

    def __init__(self, cfg: DbConfig, timescale: bool = False):
        super().__init__(cfg)
        self.timescale = timescale
        self.conn = None

    def connect(self) -> None:
        import psycopg2  # optional dependency

        self.conn = psycopg2.connect(
            host=self.cfg.host, port=self.cfg.port, dbname=self.cfg.database,
            user=self.cfg.user, password=self.cfg.password)
        self.conn.autocommit = False
        self.create_schema()
        if self.timescale:
            self._hypertables()
        log.info("postgres store ready",
                 extra={"host": self.cfg.host, "db": self.cfg.database,
                        "timescale": self.timescale})

    def _hypertables(self) -> None:
        """Convert the big tables to Timescale hypertables (idempotent)."""
        try:
            self._execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            for table in ("ticks", "structural_events", "line_break_lines",
                          "feature_snapshots"):
                self._execute(
                    f"SELECT create_hypertable('{table}', 'ts', "
                    f"chunk_time_interval => 86400, if_not_exists => TRUE, "
                    f"migrate_data => TRUE)")
            self.commit()
            log.info("timescale hypertables ready")
        except Exception as exc:  # noqa: BLE001
            # Plain PostgreSQL without the extension is a perfectly good store.
            self.conn.rollback()
            log.warning("timescaledb unavailable - using plain postgres tables",
                        extra={"err": str(exc)})

    def _executemany(self, sql: str, rows: Sequence[tuple]) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(sql, rows)

    def _execute(self, sql: str, params: Sequence = ()) -> Any:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)

    def _query(self, sql: str, params: Sequence = ()) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def commit(self) -> None:
        if self.conn is not None:
            self.conn.commit()

    def close(self) -> None:
        if self.conn is not None:
            self.flush()
            self.conn.close()
            self.conn = None


# ------------------------------------------------------------------ factory
def build_store(cfg, root: Path | None = None) -> EventStore:
    """Create the configured store, falling back to SQLite when Postgres is out."""
    from .config import PROJECT_ROOT

    dbc = DbConfig.from_config(cfg)
    root = root or PROJECT_ROOT

    if dbc.backend in ("postgres", "postgresql", "timescale", "timescaledb"):
        if not psycopg2_available():
            log.warning("psycopg2 not installed - falling back to SQLite. "
                        "Run: pip install psycopg2-binary")
            dbc.backend = "sqlite"
        else:
            ts = dbc.backend.startswith("timescale")
            store = PostgresStore(dbc, timescale=ts)
            try:
                store.connect()
                return store
            except Exception as exc:  # noqa: BLE001
                log.warning("postgres connection failed - falling back to SQLite",
                            extra={"err": str(exc)})
                dbc.backend = "sqlite"

    store = SqliteStore(dbc, root)
    store.connect()
    return store
