"""
Decision journal + engine state (prompt sections 5 and 7).

The journal is the feedback loop, so it records the *whole* lifecycle rather
than just fills:

    decision -> entry (price, size, time) -> position evolution
             -> exit (price, time, reason) -> result / P&L

Every record is tagged with the context needed to ask useful questions later:
market category, liquidity regime, time-to-resolution bucket, which target
wallet influenced it, the exit style, and the rationale the engine emitted.
``scripts/analyze_journal.py`` groups on exactly those tags.

Storage follows the same shape the upstream project uses for its SQLite layer —
one connection, an ``RLock`` around it, WAL journaling and ``synchronous=NORMAL``
— because the write path runs on the asyncio event loop and must stay in the
tens-of-microseconds range. Marks are not written per tick; only state changes
are persisted.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .models import (
    Decision, ExecutionReport, MarketFeatures, PositionView, liquidity_bucket,
    ttr_bucket,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    cycle_id      TEXT,
    action        TEXT    NOT NULL,
    market_id     TEXT,
    token_id      TEXT,
    outcome       TEXT,
    question      TEXT,
    size_usdc     REAL DEFAULT 0,
    size_shares   REAL DEFAULT 0,
    limit_price   REAL,
    confidence    REAL DEFAULT 0,
    score         REAL DEFAULT 0,
    reason        TEXT,
    exit_style    TEXT,
    wallet_influence TEXT,
    category      TEXT,
    liquidity_bucket TEXT,
    ttr_bucket    TEXT,
    mode          TEXT,
    lifecycle_id  INTEGER,
    rationale     TEXT,
    features      TEXT
);

CREATE TABLE IF NOT EXISTS executions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    decision_id   INTEGER,
    lifecycle_id  INTEGER,
    order_id      TEXT,
    token_id      TEXT,
    side          TEXT,
    requested_size REAL DEFAULT 0,
    limit_price   REAL,
    filled_size   REAL DEFAULT 0,
    avg_price     REAL DEFAULT 0,
    fee           REAL DEFAULT 0,
    status        TEXT,
    error         TEXT,
    simulated     INTEGER DEFAULT 0
);

-- One row per position, from the decision that opened it to the result.
CREATE TABLE IF NOT EXISTS lifecycles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id      TEXT NOT NULL,
    market_id     TEXT,
    outcome       TEXT,
    question      TEXT,
    status        TEXT DEFAULT 'OPEN',      -- OPEN | CLOSED
    entry_decision_id INTEGER,
    entry_ts      REAL,
    entry_price   REAL DEFAULT 0,
    entry_size    REAL DEFAULT 0,
    entry_cost    REAL DEFAULT 0,
    peak_price    REAL DEFAULT 0,
    trough_price  REAL DEFAULT 0,
    max_unrealized REAL DEFAULT 0,
    min_unrealized REAL DEFAULT 0,
    reduced_count INTEGER DEFAULT 0,
    exit_decision_id INTEGER,
    exit_ts       REAL,
    exit_price    REAL DEFAULT 0,
    exit_size     REAL DEFAULT 0,
    exit_reason   TEXT,
    exit_style    TEXT,
    realized_pnl  REAL DEFAULT 0,
    return_pct    REAL DEFAULT 0,
    hold_seconds  REAL DEFAULT 0,
    category      TEXT,
    liquidity_bucket TEXT,
    ttr_bucket    TEXT,
    wallet_influence TEXT,
    mode          TEXT
);

CREATE TABLE IF NOT EXISTS reconciliations (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    kind     TEXT NOT NULL,
    subject  TEXT,
    expected TEXT,
    actual   TEXT,
    action   TEXT,
    detail   TEXT
);

CREATE TABLE IF NOT EXISTS engine_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cycles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id      TEXT,
    ts            REAL NOT NULL,
    markets       INTEGER DEFAULT 0,
    positions     INTEGER DEFAULT 0,
    wallet_signals INTEGER DEFAULT 0,
    decisions     INTEGER DEFAULT 0,
    actionable    INTEGER DEFAULT 0,
    portfolio_value REAL DEFAULT 0,
    balance       REAL DEFAULT 0,
    min_trade_size REAL DEFAULT 0,
    flattening    INTEGER DEFAULT 0,
    duration_ms   REAL DEFAULT 0,
    errors        INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_dec_ts     ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_dec_action ON decisions(action);
CREATE INDEX IF NOT EXISTS idx_dec_token  ON decisions(token_id);
CREATE INDEX IF NOT EXISTS idx_exec_dec   ON executions(decision_id);
CREATE INDEX IF NOT EXISTS idx_life_token ON lifecycles(token_id);
CREATE INDEX IF NOT EXISTS idx_life_status ON lifecycles(status);
CREATE INDEX IF NOT EXISTS idx_recon_ts   ON reconciliations(ts);
"""


def _json(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return "{}"


class Journal:
    """SQLite-backed decision journal and persisted engine state."""

    def __init__(self, path: str | Path, mode: str = "dry_run"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- decisions -----------------------------------------------------------

    def record_decision(self, decision: Decision, cycle_id: str,
                        market: Optional[MarketFeatures] = None,
                        features: Optional[dict] = None) -> int:
        """Persist a decision and stamp its id back onto the object.

        DO_NOTHING and HOLD are recorded too. A journal that only holds the
        trades taken cannot answer "was passing on this correct?", which is half
        of what the feedback loop is for.
        """
        category = market.category if market else ""
        liq = liquidity_bucket(market.liquidity) if market else "unknown"
        ttr = ttr_bucket(market.seconds_to_resolution) if market else "unknown"
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO decisions(
                     ts, cycle_id, action, market_id, token_id, outcome,
                     question, size_usdc, size_shares, limit_price, confidence,
                     score, reason, exit_style, wallet_influence, category,
                     liquidity_bucket, ttr_bucket, mode, lifecycle_id,
                     rationale, features)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision.ts, cycle_id, decision.action.value,
                    decision.market_id, decision.token_id, decision.outcome,
                    decision.question, decision.size_usdc, decision.size_shares,
                    decision.limit_price, decision.confidence, decision.score,
                    decision.reason, decision.exit_style,
                    decision.wallet_influence, category, liq, ttr, self.mode,
                    decision.lifecycle_id, _json(decision.rationale),
                    _json(features or (market.to_dict() if market else {})),
                ),
            )
            self._conn.commit()
        decision.journal_id = cur.lastrowid
        return cur.lastrowid

    def record_execution(self, report: ExecutionReport,
                         side: str = "") -> int:
        decision = report.decision
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO executions(
                     ts, decision_id, lifecycle_id, order_id, token_id, side,
                     requested_size, limit_price, filled_size, avg_price, fee,
                     status, error, simulated)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    report.ts, decision.journal_id, decision.lifecycle_id,
                    report.order_id, decision.token_id,
                    side or decision.action.value, report.requested_size,
                    decision.limit_price, report.filled_size, report.avg_price,
                    report.fee, report.status, report.error,
                    1 if report.simulated else 0,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    # -- lifecycles ----------------------------------------------------------

    def open_lifecycle(self, decision: Decision, report: ExecutionReport,
                       market: Optional[MarketFeatures] = None) -> int:
        """Start a lifecycle row for a position that has just been entered."""
        price = report.avg_price or (decision.limit_price or 0.0)
        size = report.filled_size
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO lifecycles(
                     token_id, market_id, outcome, question, status,
                     entry_decision_id, entry_ts, entry_price, entry_size,
                     entry_cost, peak_price, trough_price, category,
                     liquidity_bucket, ttr_bucket, wallet_influence, mode)
                   VALUES(?,?,?,?,'OPEN',?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision.token_id, decision.market_id, decision.outcome,
                    decision.question, decision.journal_id, report.ts, price,
                    size, size * price, price, price,
                    market.category if market else "",
                    liquidity_bucket(market.liquidity) if market else "unknown",
                    ttr_bucket(market.seconds_to_resolution) if market
                    else "unknown",
                    decision.wallet_influence, self.mode,
                ),
            )
            self._conn.commit()
            lifecycle_id = cur.lastrowid
        decision.lifecycle_id = lifecycle_id
        return lifecycle_id

    def find_open_lifecycle(self, token_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lifecycles WHERE token_id = ? AND status = 'OPEN' "
                "ORDER BY id DESC LIMIT 1",
                (str(token_id),),
            ).fetchone()
        return dict(row) if row else None

    def open_lifecycles(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM lifecycles WHERE status = 'OPEN' ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def track_evolution(self, position: PositionView) -> None:
        """Record how far a position ran and how far it gave back.

        Only the extremes are stored, and only when they actually move, so this
        stays a rare write even though it is called for every position every
        cycle. Those extremes are what make "did we exit near the peak?"
        answerable after the fact.
        """
        if position.lifecycle_id is None:
            return
        price = position.cur_price or position.avg_price
        if price <= 0:
            return
        pnl = position.unrealized_pnl
        with self._lock:
            row = self._conn.execute(
                "SELECT peak_price, trough_price, max_unrealized, min_unrealized"
                " FROM lifecycles WHERE id = ?", (position.lifecycle_id,),
            ).fetchone()
            if row is None:
                return
            peak = max(row["peak_price"] or 0.0, price)
            trough = min(row["trough_price"] or price, price)
            max_pnl = max(row["max_unrealized"] or 0.0, pnl)
            min_pnl = min(row["min_unrealized"] or 0.0, pnl)
            if (peak == (row["peak_price"] or 0.0)
                    and trough == (row["trough_price"] or price)
                    and max_pnl == (row["max_unrealized"] or 0.0)
                    and min_pnl == (row["min_unrealized"] or 0.0)):
                return
            self._conn.execute(
                "UPDATE lifecycles SET peak_price=?, trough_price=?, "
                "max_unrealized=?, min_unrealized=? WHERE id=?",
                (peak, trough, max_pnl, min_pnl, position.lifecycle_id),
            )
            self._conn.commit()

    def record_reduction(self, lifecycle_id: int, size: float,
                         price: float, pnl: float) -> None:
        """A partial exit: banked P&L accrues, the lifecycle stays open."""
        with self._lock:
            self._conn.execute(
                "UPDATE lifecycles SET reduced_count = reduced_count + 1, "
                "realized_pnl = realized_pnl + ?, exit_size = exit_size + ? "
                "WHERE id = ?",
                (pnl, size, lifecycle_id),
            )
            self._conn.commit()

    def close_lifecycle(self, lifecycle_id: int, exit_price: float,
                        exit_size: float, realized_pnl: float,
                        reason: str, exit_style: str,
                        exit_decision_id: Optional[int] = None) -> None:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT entry_ts, entry_cost, realized_pnl FROM lifecycles "
                "WHERE id = ?", (lifecycle_id,),
            ).fetchone()
            if row is None:
                return
            total_pnl = (row["realized_pnl"] or 0.0) + realized_pnl
            cost = row["entry_cost"] or 0.0
            self._conn.execute(
                """UPDATE lifecycles SET status='CLOSED', exit_decision_id=?,
                     exit_ts=?, exit_price=?, exit_size = exit_size + ?,
                     exit_reason=?, exit_style=?, realized_pnl=?, return_pct=?,
                     hold_seconds=? WHERE id=?""",
                (
                    exit_decision_id, now, exit_price, exit_size, reason,
                    exit_style, total_pnl,
                    (total_pnl / cost) if cost else 0.0,
                    now - (row["entry_ts"] or now), lifecycle_id,
                ),
            )
            self._conn.commit()

    # -- reconciliation ------------------------------------------------------

    def record_reconciliation(self, kind: str, subject: str = "",
                              expected: Any = None, actual: Any = None,
                              action: str = "", detail: Any = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO reconciliations(
                     ts, kind, subject, expected, actual, action, detail)
                   VALUES(?,?,?,?,?,?,?)""",
                (time.time(), kind, subject, _json(expected), _json(actual),
                 action, _json(detail)),
            )
            self._conn.commit()
            return cur.lastrowid

    # -- cycle summaries -----------------------------------------------------

    def record_cycle(self, cycle_id: str, summary: dict) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO cycles(
                     cycle_id, ts, markets, positions, wallet_signals,
                     decisions, actionable, portfolio_value, balance,
                     min_trade_size, flattening, duration_ms, errors)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cycle_id, time.time(), summary.get("markets", 0),
                    summary.get("positions", 0),
                    summary.get("walletSignals", 0),
                    summary.get("decisions", 0), summary.get("actionable", 0),
                    summary.get("portfolioValue", 0.0),
                    summary.get("balance", 0.0),
                    summary.get("minTradeSize", 0.0),
                    1 if summary.get("flattening") else 0,
                    summary.get("durationMs", 0.0), summary.get("errors", 0),
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    # -- engine state (doubling baseline, progression index, …) --------------

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM engine_state WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO engine_state(key, value, ts) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "ts=excluded.ts",
                (key, _json(value), time.time()),
            )
            self._conn.commit()

    # -- reads for the analysis script ---------------------------------------

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def execute(self, sql: str, params: tuple = ()) -> int:
        """Run a statement that modifies rows, and commit it.

        Separate from :meth:`query` on purpose: a DML statement run through the
        read path would open a transaction and never commit it, so the change
        would be silently discarded. Used by the reconciler's targeted repairs.
        """
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount
