"""The experiment registry (§13, §36).

Two jobs:

  1. Never rediscover the same failed strategy. Every candidate is keyed by
     (spec_hash, dataset_version, engine_version) so a re-run skips what it has
     already answered and a changed cost model correctly invalidates it.

  2. Make the multiple-testing denominator real (§34). The registry knows how
     many hypotheses were tested to produce a reported winner, so a leaderboard
     can never quote a p-value without its denominator.

Every row is reproducible: the strategy spec is stored verbatim as JSON, so any
result can be re-executed from the registry alone.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .config import ENGINE_VERSION
from .strategy import CopyStrategy

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_hash       TEXT NOT NULL,
    params_hash     TEXT NOT NULL,
    wallet          TEXT NOT NULL,
    engine_version  TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    spec_json       TEXT NOT NULL,
    status          TEXT NOT NULL,
    score           REAL,
    oos_p           REAL,
    train_json      TEXT,
    valid_json      TEXT,
    test_json       TEXT,
    robust_json     TEXT,
    walk_json       TEXT,
    cross_json      TEXT,
    created_ts      INTEGER NOT NULL,
    UNIQUE (spec_hash, engine_version, dataset_version)
);
CREATE INDEX IF NOT EXISTS ix_exp_wallet ON experiments (wallet);
CREATE INDEX IF NOT EXISTS ix_exp_status ON experiments (status);
CREATE INDEX IF NOT EXISTS ix_exp_score  ON experiments (score DESC);
CREATE INDEX IF NOT EXISTS ix_exp_params ON experiments (params_hash);

CREATE TABLE IF NOT EXISTS passes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts      INTEGER NOT NULL,
    finished_ts     INTEGER,
    wallets         INTEGER,
    hypotheses      INTEGER,
    validated       INTEGER,
    fdr_threshold   REAL,
    notes           TEXT
);
"""

VALID_STATUSES = {
    "INSUFFICIENT_EVIDENCE", "FAILED", "NOT_SIGNIFICANT", "NO_WALLET_ALPHA",
    "OVERFIT", "CONCENTRATED", "UNSTABLE", "VALIDATED",
}


class Registry:
    def __init__(self, path: Path, dataset_version: str) -> None:
        self.path = Path(path)
        self.dataset_version = dataset_version
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ reads
    def seen(self, spec_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM experiments WHERE spec_hash=? AND engine_version=? "
            "AND dataset_version=?",
            (spec_hash, ENGINE_VERSION, self.dataset_version),
        ).fetchone()
        return row is not None

    def count(self, status: str | None = None) -> int:
        if status:
            return self.conn.execute(
                "SELECT COUNT(*) FROM experiments WHERE status=?", (status,)
            ).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]

    def all_pvalues(self) -> list[float]:
        return [
            r[0] for r in self.conn.execute(
                "SELECT oos_p FROM experiments WHERE oos_p IS NOT NULL"
            )
        ]

    def leaderboard(self, limit: int = 25, status: str | None = None) -> list[dict]:
        sql = "SELECT wallet, spec_hash, status, score, oos_p, spec_json, test_json FROM experiments"
        params: list = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY score DESC, oos_p ASC LIMIT ?"
        params.append(limit)
        out = []
        for w, sh, stt, sc, p, spec, test in self.conn.execute(sql, params):
            out.append({
                "wallet": w, "spec_hash": sh, "status": stt,
                "score": sc, "oos_p": p,
                "spec": json.loads(spec),
                "test": json.loads(test) if test else {},
            })
        return out

    def by_params_hash(self, params_hash: str) -> list[dict]:
        """All wallets that ran the same transformation — the §9 cross-wallet view."""
        rows = self.conn.execute(
            "SELECT wallet, status, score, oos_p, test_json FROM experiments "
            "WHERE params_hash=? ORDER BY score DESC",
            (params_hash,),
        ).fetchall()
        return [
            {"wallet": w, "status": s, "score": sc, "oos_p": p,
             "test": json.loads(t) if t else {}}
            for w, s, sc, p, t in rows
        ]

    # ----------------------------------------------------------------- writes
    def record(
        self,
        strategy: CopyStrategy,
        status: str,
        score: float,
        oos_p: float,
        train: dict, valid: dict, test: dict,
        robust: dict | None = None,
        walk: list | None = None,
        cross: list | None = None,
    ) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        self.conn.execute(
            "INSERT OR REPLACE INTO experiments "
            "(spec_hash, params_hash, wallet, engine_version, dataset_version, "
            " spec_json, status, score, oos_p, train_json, valid_json, test_json, "
            " robust_json, walk_json, cross_json, created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                strategy.spec_hash(), strategy.params_only_hash(), strategy.wallet,
                ENGINE_VERSION, self.dataset_version,
                json.dumps(strategy.spec(), sort_keys=True), status, score, oos_p,
                json.dumps(train), json.dumps(valid), json.dumps(test),
                json.dumps(robust) if robust else None,
                json.dumps(walk) if walk else None,
                json.dumps(cross) if cross else None,
                int(time.time()),
            ),
        )

    def commit(self) -> None:
        self.conn.commit()

    def open_pass(self, fdr: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO passes (started_ts, fdr_threshold) VALUES (?,?)",
            (int(time.time()), fdr),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def close_pass(self, pass_id: int, wallets: int, hypotheses: int,
                   validated: int, notes: str = "") -> None:
        self.conn.execute(
            "UPDATE passes SET finished_ts=?, wallets=?, hypotheses=?, validated=?, "
            "notes=? WHERE id=?",
            (int(time.time()), wallets, hypotheses, validated, notes, pass_id),
        )
        self.conn.commit()
