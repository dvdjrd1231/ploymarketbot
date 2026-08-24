"""
SQLite persistence layer.

Ported from the desktop app and extended with an ``orders`` table so pending /
live / cancelled orders can be shown and reconciled against the exchange.

Design notes for the web build
------------------------------
The engine runs on the asyncio event loop, so nothing here may block for long.
Two things make that safe:

  * WAL journal mode + ``synchronous=NORMAL`` — writes return without waiting on
    an fsync, keeping every call in the tens-of-microseconds range.
  * Live mark prices are **not** written on every tick. They live in the
    in-memory position registry (see ``services/engine.py``) and are persisted
    only when a position actually changes state. The old app rewrote rows on a
    3-second timer, which is what made the UI stutter under load.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .models import (
    CopiedTrade, OrderRecord, OrderStatus, Side, TradeStatus,
)
from .paths import app_data_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_trades (
    trade_id     TEXT PRIMARY KEY,
    processed_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS copied_trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    target_trade_id TEXT,
    market_id      TEXT,
    token_id       TEXT,
    outcome        TEXT,
    market_question TEXT,
    side           TEXT,
    mode           TEXT,
    target_price   REAL,
    target_usdc    REAL,
    my_price       REAL,
    my_size        REAL,
    my_usdc        REAL,
    status         TEXT,
    order_id       TEXT,
    reason         TEXT,
    opened_ts      INTEGER,
    closed_ts      INTEGER,
    market_end_ts  INTEGER,
    realized_pnl   REAL DEFAULT 0,
    current_price  REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT,
    trade_db_id     INTEGER,
    token_id        TEXT,
    market_id       TEXT,
    market_question TEXT,
    outcome         TEXT,
    side            TEXT,
    price           REAL,
    size            REAL,
    filled_size     REAL DEFAULT 0,
    status          TEXT,
    error           TEXT,
    created_ts      INTEGER,
    updated_ts      INTEGER
);

CREATE TABLE IF NOT EXISTS activity_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_copied_status ON copied_trades(status);
CREATE INDEX IF NOT EXISTS idx_copied_opened ON copied_trades(opened_ts);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_oid ON orders(order_id);
CREATE INDEX IF NOT EXISTS idx_log_ts ON activity_log(ts);
"""

# Columns added after the original desktop schema shipped. Applied additively so
# an existing copybot.sqlite3 keeps working if the user points us at it.
_MIGRATIONS = [
    ("copied_trades", "current_price", "REAL DEFAULT 0"),
]


class Database:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or (app_data_dir() / "copybot.sqlite3")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        for table, column, decl in _MIGRATIONS:
            cols = {
                r["name"]
                for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in cols:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def reset_session(self, clear_log: bool = True) -> None:
        """Wipe trades / orders / de-dup records for a clean session.

        Used when a paper-trading session starts without "restore previous
        session", so the dashboard, P/L and history all begin empty.
        """
        with self._lock:
            self._conn.execute("DELETE FROM copied_trades")
            self._conn.execute("DELETE FROM processed_trades")
            self._conn.execute("DELETE FROM orders")
            if clear_log:
                self._conn.execute("DELETE FROM activity_log")
            self._conn.commit()

    # -- de-duplication ------------------------------------------------------

    def is_processed(self, trade_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM processed_trades WHERE trade_id = ?", (trade_id,)
            )
            return cur.fetchone() is not None

    def mark_processed(self, trade_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO processed_trades(trade_id, processed_ts) "
                "VALUES(?, ?)",
                (trade_id, int(time.time())),
            )
            self._conn.commit()

    # -- copied trades -------------------------------------------------------

    def insert_copied_trade(self, t: CopiedTrade) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO copied_trades(
                    target_trade_id, market_id, token_id, outcome, market_question,
                    side, mode, target_price, target_usdc, my_price, my_size,
                    my_usdc, status, order_id, reason, opened_ts, closed_ts,
                    market_end_ts, realized_pnl, current_price)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    t.target_trade_id, t.market_id, t.token_id, t.outcome,
                    t.market_question, t.side.value, t.mode, t.target_price,
                    t.target_usdc, t.my_price, t.my_size, t.my_usdc,
                    t.status.value, t.order_id, t.reason, t.opened_ts,
                    t.closed_ts, t.market_end_ts, t.realized_pnl, t.current_price,
                ),
            )
            self._conn.commit()
            t.db_id = cur.lastrowid
            return cur.lastrowid

    def update_copied_trade(self, t: CopiedTrade) -> None:
        if t.db_id is None:
            return
        with self._lock:
            self._conn.execute(
                """UPDATE copied_trades SET
                    my_price=?, my_size=?, my_usdc=?, status=?, order_id=?,
                    reason=?, closed_ts=?, realized_pnl=?, current_price=?,
                    market_end_ts=?, market_question=?
                   WHERE id=?""",
                (
                    t.my_price, t.my_size, t.my_usdc, t.status.value, t.order_id,
                    t.reason, t.closed_ts, t.realized_pnl, t.current_price,
                    t.market_end_ts, t.market_question, t.db_id,
                ),
            )
            self._conn.commit()

    def get_open_trades(self) -> list[CopiedTrade]:
        return self._query_trades("status = ?", (TradeStatus.OPEN.value,))

    def get_closed_trades(self, limit: int = 500) -> list[CopiedTrade]:
        return self._query_trades(
            "status != ?", (TradeStatus.OPEN.value,), order="opened_ts DESC",
            limit=limit,
        )

    def count_open_trades(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS n FROM copied_trades WHERE status = ?",
                (TradeStatus.OPEN.value,),
            )
            return int(cur.fetchone()["n"])

    def _query_trades(self, where: str, params, order: str = "opened_ts ASC",
                      limit: Optional[int] = None) -> list[CopiedTrade]:
        sql = f"SELECT * FROM copied_trades WHERE {where} ORDER BY {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_trade(r) for r in rows]

    @staticmethod
    def _row_to_trade(r: sqlite3.Row) -> CopiedTrade:
        t = CopiedTrade(
            target_trade_id=r["target_trade_id"],
            market_id=r["market_id"],
            token_id=r["token_id"],
            outcome=r["outcome"],
            market_question=r["market_question"],
            side=Side(r["side"]),
            mode=r["mode"],
            target_price=r["target_price"] or 0.0,
            target_usdc=r["target_usdc"] or 0.0,
            my_price=r["my_price"] or 0.0,
            my_size=r["my_size"] or 0.0,
            my_usdc=r["my_usdc"] or 0.0,
            status=TradeStatus(r["status"]),
            order_id=r["order_id"] or "",
            reason=r["reason"] or "",
            opened_ts=r["opened_ts"] or 0,
            closed_ts=r["closed_ts"],
            market_end_ts=r["market_end_ts"],
            realized_pnl=r["realized_pnl"] or 0.0,
            current_price=r["current_price"] or 0.0,
        )
        t.db_id = r["id"]
        return t

    # -- orders --------------------------------------------------------------

    def insert_order(self, o: OrderRecord) -> int:
        o.created_ts = o.created_ts or int(time.time())
        o.updated_ts = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO orders(
                    order_id, trade_db_id, token_id, market_id, market_question,
                    outcome, side, price, size, filled_size, status, error,
                    created_ts, updated_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    o.order_id, o.trade_db_id, o.token_id, o.market_id,
                    o.market_question, o.outcome, o.side.value, o.price, o.size,
                    o.filled_size, o.status.value, o.error, o.created_ts,
                    o.updated_ts,
                ),
            )
            self._conn.commit()
            o.db_id = cur.lastrowid
            return cur.lastrowid

    def update_order(self, o: OrderRecord) -> None:
        if o.db_id is None:
            return
        o.updated_ts = int(time.time())
        with self._lock:
            self._conn.execute(
                """UPDATE orders SET order_id=?, filled_size=?, status=?, error=?,
                       price=?, size=?, updated_ts=? WHERE id=?""",
                (o.order_id, o.filled_size, o.status.value, o.error, o.price,
                 o.size, o.updated_ts, o.db_id),
            )
            self._conn.commit()

    def get_orders(self, limit: int = 300) -> list[OrderRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM orders ORDER BY created_ts DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_order(r) for r in rows]

    def get_pending_orders(self) -> list[OrderRecord]:
        pending = (OrderStatus.PENDING.value, OrderStatus.LIVE.value,
                   OrderStatus.MATCHED.value)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM orders WHERE status IN ({','.join('?' * len(pending))}) "
                "ORDER BY created_ts DESC",
                pending,
            ).fetchall()
        return [self._row_to_order(r) for r in rows]

    @staticmethod
    def _row_to_order(r: sqlite3.Row) -> OrderRecord:
        o = OrderRecord(
            order_id=r["order_id"] or "",
            token_id=r["token_id"] or "",
            market_id=r["market_id"] or "",
            market_question=r["market_question"] or "",
            outcome=r["outcome"] or "",
            side=Side(r["side"]),
            price=r["price"] or 0.0,
            size=r["size"] or 0.0,
            filled_size=r["filled_size"] or 0.0,
            status=OrderStatus(r["status"]),
            error=r["error"] or "",
            trade_db_id=r["trade_db_id"],
            created_ts=r["created_ts"] or 0,
            updated_ts=r["updated_ts"] or 0,
        )
        o.db_id = r["id"]
        return o

    # -- statistics ----------------------------------------------------------

    def daily_stats(self, day_start_ts: int) -> dict:
        """Counts + realized pnl for trades opened since ``day_start_ts``."""
        with self._lock:
            row = self._conn.execute(
                """SELECT
                     SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END)    AS open_n,
                     SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END)  AS closed_n,
                     SUM(CASE WHEN status='SKIPPED' THEN 1 ELSE 0 END) AS skipped_n,
                     SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END)  AS failed_n,
                     COALESCE(SUM(realized_pnl), 0)                    AS pnl
                   FROM copied_trades WHERE opened_ts >= ?""",
                (day_start_ts,),
            ).fetchone()
            allrow = self._conn.execute(
                """SELECT COALESCE(SUM(realized_pnl), 0) AS pnl,
                          SUM(CASE WHEN status='CLOSED' AND realized_pnl > 0
                                   THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) AS settled
                   FROM copied_trades"""
            ).fetchone()
        settled = allrow["settled"] or 0
        wins = allrow["wins"] or 0
        return {
            "open": row["open_n"] or 0,
            "closed": row["closed_n"] or 0,
            "skipped": row["skipped_n"] or 0,
            "failed": row["failed_n"] or 0,
            "realizedPnlToday": row["pnl"] or 0.0,
            "realizedPnlTotal": allrow["pnl"] or 0.0,
            "settledTotal": settled,
            "winsTotal": wins,
            "winRate": (wins / settled * 100.0) if settled else 0.0,
        }

    # -- activity log --------------------------------------------------------

    def add_log(self, level: str, message: str, ts: Optional[int] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO activity_log(ts, level, message) VALUES(?,?,?)",
                (int(ts or time.time()), level, message),
            )
            self._conn.commit()

    def recent_logs(self, limit: int = 400) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, level, message FROM activity_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def clear_logs(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM activity_log")
            self._conn.commit()
