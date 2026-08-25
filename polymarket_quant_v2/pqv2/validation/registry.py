"""The research log: every strategy ever tested, with the evidence behind it.

Two jobs, and the second is the one people forget:

  1. Remember what was tried, so a sweep does not re-test the same hypothesis
     and quietly inflate its own denominator.
  2. Remember the DENOMINATOR. The number of hypotheses tested is part of the
     result. A registry that stores only the winners is a machine for
     generating false discoveries with an audit trail.

Its own database, under the V2 work directory. The V1 library.sqlite3 is opened
read-only when comparing, never written.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    strategy_id   TEXT NOT NULL,
    wallet        TEXT NOT NULL,
    spec_hash     TEXT NOT NULL,
    params_hash   TEXT NOT NULL,
    family        TEXT DEFAULT '',
    label         TEXT DEFAULT '',
    describe      TEXT DEFAULT '',
    spec          TEXT NOT NULL,
    status        TEXT NOT NULL,
    lifecycle     TEXT NOT NULL DEFAULT 'RESEARCH',
    reasons       TEXT DEFAULT '[]',
    p_value       REAL DEFAULT 1.0,
    score         REAL DEFAULT 0.0,
    is_sample     TEXT DEFAULT '{}',
    oos           TEXT DEFAULT '{}',
    alpha         TEXT DEFAULT '{}',
    walkforward   TEXT DEFAULT '{}',
    robustness    TEXT DEFAULT '{}',
    train_from    INTEGER DEFAULT 0,
    train_to      INTEGER DEFAULT 0,
    test_from     INTEGER DEFAULT 0,
    test_to       INTEGER DEFAULT 0,
    created_ts    REAL NOT NULL,
    updated_ts    REAL NOT NULL,
    PRIMARY KEY (spec_hash, wallet)
);
CREATE INDEX IF NOT EXISTS ix_st_status ON strategies(status);
CREATE INDEX IF NOT EXISTS ix_st_params ON strategies(params_hash);
CREATE INDEX IF NOT EXISTS ix_st_score  ON strategies(score DESC);

-- The denominator. One row per discovery pass.
CREATE TABLE IF NOT EXISTS passes (
    pass_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts    REAL, finished_ts REAL,
    wallets       INTEGER, hypotheses INTEGER,
    selection_penalty INTEGER DEFAULT 0,
    bh_threshold  REAL, bh_significant INTEGER,
    validated     INTEGER, notes TEXT DEFAULT '',
    config        TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS families (
    family_id TEXT PRIMARY KEY, pass_id INTEGER,
    payload TEXT NOT NULL, updated_ts REAL
);

-- Lifecycle transitions, append-only. A promotion with no row here did not
-- happen through the ladder.
CREATE TABLE IF NOT EXISTS promotions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT, wallet TEXT, from_lc TEXT, to_lc TEXT,
    status TEXT, evidence TEXT, ts REAL
);
"""


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- passes --------------------------------------------------------------
    def open_pass(self, config: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO passes (started_ts, config) VALUES (?, ?)",
            (time.time(), json.dumps(config, default=str)))
        self.conn.commit()
        return int(cur.lastrowid)

    def close_pass(self, pass_id: int, **fields) -> None:
        fields["finished_ts"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE passes SET {cols} WHERE pass_id = ?",
                          list(fields.values()) + [pass_id])
        self.conn.commit()

    # -- strategies ----------------------------------------------------------
    def record(self, strategy, verdict, *, train: tuple = (0, 0),
               test: tuple = (0, 0)) -> None:
        now = time.time()
        self.conn.execute(
            """INSERT INTO strategies
               (strategy_id, wallet, spec_hash, params_hash, family, label,
                describe, spec, status, lifecycle, reasons, p_value, score,
                is_sample, oos, alpha, walkforward, robustness,
                train_from, train_to, test_from, test_to, created_ts, updated_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(spec_hash, wallet) DO UPDATE SET
                 status=excluded.status, reasons=excluded.reasons,
                 p_value=excluded.p_value, score=excluded.score,
                 is_sample=excluded.is_sample, oos=excluded.oos,
                 alpha=excluded.alpha, walkforward=excluded.walkforward,
                 robustness=excluded.robustness, updated_ts=excluded.updated_ts
            """,
            (verdict.strategy_id, strategy.wallet, strategy.spec_hash(),
             strategy.params_only_hash(), strategy.family, strategy.label,
             verdict.describe, json.dumps(strategy.spec(), default=str),
             verdict.status, "RESEARCH", json.dumps(verdict.reasons),
             verdict.p_value, verdict.score, json.dumps(verdict.is_sample),
             json.dumps(verdict.oos), json.dumps(verdict.alpha),
             json.dumps(verdict.walkforward), json.dumps(verdict.robustness),
             train[0], train[1], test[0], test[1], now, now))

    def record_many(self, items) -> int:
        n = 0
        for strategy, verdict, train, test in items:
            self.record(strategy, verdict, train=train, test=test)
            n += 1
        self.conn.commit()
        return n

    def seen(self, spec_hash: str, wallet: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM strategies WHERE spec_hash=? AND wallet=?",
            (spec_hash, wallet)).fetchone() is not None

    def leaderboard(self, *, status: str | None = None, limit: int = 25) -> list:
        sql = "SELECT * FROM strategies"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY score DESC, p_value ASC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params)]

    def status_histogram(self) -> list:
        return [(r[0], r[1]) for r in self.conn.execute(
            "SELECT status, COUNT(*) n FROM strategies GROUP BY status "
            "ORDER BY n DESC")]

    def tradable(self) -> list:
        from .validate import TRADABLE_STATUSES
        marks = ",".join("?" * len(TRADABLE_STATUSES))
        return [dict(r) for r in self.conn.execute(
            f"SELECT * FROM strategies WHERE status IN ({marks}) "
            "ORDER BY score DESC", tuple(TRADABLE_STATUSES))]

    def promote(self, strategy_id: str, wallet: str, to_lc: str,
                status: str, evidence: str) -> None:
        row = self.conn.execute(
            "SELECT lifecycle FROM strategies WHERE strategy_id=? AND wallet=?",
            (strategy_id, wallet)).fetchone()
        from_lc = row["lifecycle"] if row else "RESEARCH"
        self.conn.execute(
            "UPDATE strategies SET lifecycle=?, updated_ts=? "
            " WHERE strategy_id=? AND wallet=?",
            (to_lc, time.time(), strategy_id, wallet))
        self.conn.execute(
            "INSERT INTO promotions (strategy_id, wallet, from_lc, to_lc, "
            "status, evidence, ts) VALUES (?,?,?,?,?,?,?)",
            (strategy_id, wallet, from_lc, to_lc, status, evidence, time.time()))
        self.conn.commit()

    def save_families(self, pass_id: int, families) -> None:
        for fam in families:
            self.conn.execute(
                "INSERT OR REPLACE INTO families (family_id, pass_id, payload,"
                " updated_ts) VALUES (?,?,?,?)",
                (fam.family_id, pass_id, json.dumps(fam.to_dict()), time.time()))
        self.conn.commit()

    def families(self) -> list:
        return [json.loads(r["payload"])
                for r in self.conn.execute("SELECT payload FROM families")]

    def close(self) -> None:
        self.conn.close()
