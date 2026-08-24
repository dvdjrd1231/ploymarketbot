"""
The intel store: raw wallet/market observations, kept broad on purpose.

Ingestion records **every wallet observed trading**, not a configured shortlist.
That is the whole point — the analytical layer cannot discover that an unranked
wallet moved first if the ingestion layer already threw that wallet away. Each
observation keeps its identity (address), timestamp, market, outcome, side,
price, size and USDC value, so any feature we later decide we want is derivable
without re-fetching history we can no longer get.

Storage follows the same discipline as :mod:`pqb.journal` — one connection under
an ``RLock``, WAL journaling, ``synchronous=NORMAL`` — because writes happen on
the event loop. Two things keep it bounded over a long session:

  * raw trades are pruned past a retention horizon, and
  * before pruning, per-market hourly rollups are written, so the flow baselines
    an anomaly detector needs survive the raw rows they were computed from.

Nothing here interprets anything. Ranking, scoring and anomaly detection all
read from this store and live in their own modules.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from ..models import AnomalySignal, WalletIntel, WalletTrade

_SCHEMA = """
-- Every observed trade by any wallet, normalised. Identity is never dropped.
CREATE TABLE IF NOT EXISTS wallet_trades (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet    TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    market_id TEXT,
    token_id  TEXT,
    outcome   TEXT,
    side      TEXT,
    price     REAL DEFAULT 0,
    size      REAL DEFAULT 0,
    usdc      REAL DEFAULT 0,
    question  TEXT,
    tx        TEXT,
    source    TEXT,
    -- What KIND of event this is. Defaults to TRADE so every existing row and
    -- every existing query mean exactly what they did before; non-trade
    -- lifecycle events (REDEEM, MERGE, SPLIT) arrive through
    -- `record_activity` and carry an empty `side`, so anything filtering on
    -- side already excludes them.
    event_type TEXT DEFAULT 'TRADE'
);

-- The same trade can arrive from the per-market sweep and the global sweep in
-- the same cycle. A natural key over the trade's own facts de-duplicates it
-- without needing the transaction hash, which not every endpoint returns.
CREATE UNIQUE INDEX IF NOT EXISTS idx_wt_natural
    ON wallet_trades(wallet, ts, token_id, side, price, size);
CREATE INDEX IF NOT EXISTS idx_wt_wallet ON wallet_trades(wallet, ts);
CREATE INDEX IF NOT EXISTS idx_wt_market ON wallet_trades(market_id, ts);
CREATE INDEX IF NOT EXISTS idx_wt_token  ON wallet_trades(token_id, ts);
CREATE INDEX IF NOT EXISTS idx_wt_ts     ON wallet_trades(ts);

-- Hourly per-market rollups. Written before raw rows are pruned so a market's
-- flow baseline outlives the trades it was measured from.
CREATE TABLE IF NOT EXISTS market_flow (
    market_id TEXT NOT NULL,
    hour      INTEGER NOT NULL,          -- unix hour bucket
    trades    INTEGER DEFAULT 0,
    wallets   INTEGER DEFAULT 0,
    gross_usdc REAL DEFAULT 0,
    net_usdc  REAL DEFAULT 0,
    PRIMARY KEY (market_id, hour)
);
CREATE INDEX IF NOT EXISTS idx_mf_hour ON market_flow(hour);

-- How each market actually resolved. This is the ground truth wallet skill is
-- scored against, so it is stored separately from the trades and never pruned.
CREATE TABLE IF NOT EXISTS resolutions (
    token_id  TEXT PRIMARY KEY,
    market_id TEXT,
    price     REAL NOT NULL,             -- 1.0 = this outcome won, 0.0 = lost
    -- WHEN WE WROTE THE ROW. Not when the market settled. Keeping the two
    -- apart matters: `ts` is a fact about this database's polling schedule,
    -- and a backfill run today stamps a market that settled last year with
    -- today's date. Anything time-aware that reads `ts` as the settlement
    -- moment is reading our own bookkeeping as market data.
    ts        REAL NOT NULL,
    -- WHEN THE MARKET ACTUALLY SETTLED, from the source record. Zero when
    -- the source did not carry one, in which case `settled_source` says so
    -- rather than a plausible-looking number standing in silently.
    settled_ts     REAL DEFAULT 0,
    settled_source TEXT DEFAULT ''       -- gamma_closed|gamma_end|last_trade|''
);
CREATE INDEX IF NOT EXISTS idx_res_market ON resolutions(market_id);

-- The ranking layer's output, cached so a restart and the CLI can both read it
-- without recomputing, and so rank movement is observable over time.
CREATE TABLE IF NOT EXISTS wallet_scores (
    wallet    TEXT PRIMARY KEY,
    label     TEXT,
    rank      INTEGER DEFAULT 0,
    score     REAL DEFAULT 0,
    raw_score REAL DEFAULT 0,
    confidence REAL DEFAULT 0,
    sample    INTEGER DEFAULT 0,
    trades    INTEGER DEFAULT 0,
    markets   INTEGER DEFAULT 0,
    win_rate  REAL DEFAULT 0,
    avg_return REAL DEFAULT 0,
    realized_usdc REAL DEFAULT 0,
    avg_usdc  REAL DEFAULT 0,
    std_usdc  REAL DEFAULT 0,
    max_usdc  REAL DEFAULT 0,
    trades_per_day REAL DEFAULT 0,
    hedge_rate REAL DEFAULT 0,
    first_seen INTEGER DEFAULT 0,
    last_seen  INTEGER DEFAULT 0,
    in_cohort INTEGER DEFAULT 0,
    pinned    INTEGER DEFAULT 0,
    updated_ts REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ws_rank ON wallet_scores(rank);

-- Every detection, kept so the system can demonstrate what it found and when,
-- and so an anomaly's later outcome can be looked up.
CREATE TABLE IF NOT EXISTS anomalies (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    kind      TEXT NOT NULL,
    subject   TEXT,
    wallet    TEXT,
    label     TEXT,
    market_id TEXT,
    token_id  TEXT,
    outcome   TEXT,
    z         REAL DEFAULT 0,
    strength  REAL DEFAULT 0,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_an_ts   ON anomalies(ts);
CREATE INDEX IF NOT EXISTS idx_an_kind ON anomalies(kind);
CREATE INDEX IF NOT EXISTS idx_an_tok  ON anomalies(token_id);

-- Raw events, verbatim (his step 9: RAW + normalized + snapshot). The trade
-- rows and book looks exactly as the feed delivered them, so a different
-- Market-State algorithm can be replayed later without reacquiring anything.
-- Pruned on the same retention horizon as raw trades.
CREATE TABLE IF NOT EXISTS raw_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,   -- doubles as event_id
    ts        REAL NOT NULL,
    kind      TEXT NOT NULL,                       -- 'trade' | 'book'
    market_id TEXT,
    token_id  TEXT,
    payload   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_token ON raw_events(token_id, ts);
CREATE INDEX IF NOT EXISTS idx_raw_ts    ON raw_events(ts);

-- Cheap key-value stats, written by the pipeline's periodic passes so readers
-- never need a full-table scan. A dashboard COUNT(DISTINCT wallet) over a
-- gigabyte of trades froze the GUI thread for seconds per refresh; these are
-- the same numbers at O(1).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value REAL NOT NULL,
    ts    REAL NOT NULL
);

-- The research time series: one feature vector per token per capture tick.
-- This is what strategy discovery is run over, and there is no way to
-- reconstruct it after the fact — an order book that existed for four seconds
-- three weeks ago is simply gone — so it is captured as it happens.
CREATE TABLE IF NOT EXISTS research_rows (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    token_id  TEXT NOT NULL,
    market_id TEXT,
    outcome   TEXT,
    category  TEXT,
    features  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rr_token ON research_rows(token_id, ts);
CREATE INDEX IF NOT EXISTS idx_rr_ts    ON research_rows(ts);
"""

_SCORE_FIELDS = (
    "label", "rank", "score", "raw_score", "confidence", "sample", "trades",
    "markets", "win_rate", "avg_return", "realized_usdc", "avg_usdc",
    "std_usdc", "max_usdc", "trades_per_day", "hedge_rate", "first_seen", "last_seen",
    "in_cohort", "pinned",
)


def _json(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return "{}"


class IntelStore:
    """SQLite-backed store of observed wallet activity and derived intel."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        """Add columns introduced after a database was first created.

        ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already
        exists, so a new column in the schema above is invisible to anyone
        upgrading — every write then fails with "no such column" and the whole
        analytical layer goes quiet while the bot otherwise appears healthy.

        Deriving the wanted columns from the schema text keeps this honest:
        adding a column above is all that is needed, here and for existing
        installs both.
        """
        import re

        for table in ("wallet_scores", "wallet_trades", "anomalies",
                      "research_rows", "market_flow", "resolutions"):
            block = re.search(
                rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);",
                _SCHEMA, re.S)
            if not block:
                continue
            wanted = {}
            for line in block.group(1).splitlines():
                # Strip a trailing inline comment BEFORE parsing. Without
                # this, a column documented as `foo TEXT DEFAULT '' -- note`
                # produced `ALTER TABLE t ADD COLUMN foo TEXT DEFAULT ''
                # -- note`, where the comment swallows the statement
                # terminator and SQLite refuses the whole thing with
                # "incomplete input". The failure is at startup, so the
                # entire analytical layer goes down — and the cause is a
                # comment, which is the last place anyone looks.
                line = line.split("--")[0].strip().rstrip(",")
                if not line or line.startswith(("PRIMARY", "UNIQUE",
                                                "FOREIGN", "CHECK")):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    wanted[parts[0]] = " ".join(parts[1:])
            have = {r[1] for r in self._conn.execute(
                f"PRAGMA table_info({table})")}
            if not have:
                continue
            for column, decl in wanted.items():
                if column not in have:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- ingestion -----------------------------------------------------------

    def record_trades(self, trades: Iterable[WalletTrade]) -> int:
        """Persist observations, ignoring ones already seen.

        Returns the number of genuinely new rows, which is what the caller logs
        — "polled 400 trades, 12 new" is the useful line, not the raw count.
        """
        rows = [
            (t.wallet.lower(), int(t.ts), t.market_id, t.token_id, t.outcome,
             t.side, float(t.price), float(t.size), float(t.usdc),
             t.question[:200], t.tx, t.source)
            for t in trades if t.wallet
        ]
        if not rows:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                """INSERT OR IGNORE INTO wallet_trades(
                     wallet, ts, market_id, token_id, outcome, side, price,
                     size, usdc, question, tx, source)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
            self._conn.commit()
            return self._conn.total_changes - before

    def record_resolution(self, token_id: str, market_id: str,
                          price: float, settled_ts: float = 0.0,
                          settled_source: str = "") -> None:
        """Record how an outcome settled. Idempotent; first write wins.

        Deliberately not an upsert: a market's settlement is a fact, and a later
        transient read returning something different should not overwrite the
        value every historical wallet score was computed against.

        `settled_ts` is when the MARKET settled, taken from the source record.
        A later call MAY fill it in on a row that has none — learning the real
        date is new information about the market, whereas a changed price would
        be a contradiction — but it can never overwrite one already stored.
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO resolutions(token_id, market_id, price, "
                "ts, settled_ts, settled_source) VALUES(?,?,?,?,?,?)",
                (str(token_id), str(market_id), float(price), time.time(),
                 float(settled_ts or 0.0), str(settled_source or "")))
            if settled_ts:
                self._conn.execute(
                    "UPDATE resolutions SET settled_ts=?, settled_source=? "
                    "WHERE token_id=? AND COALESCE(settled_ts,0) <= 0",
                    (float(settled_ts), str(settled_source), str(token_id)))
            self._conn.commit()

    def settlement_times(self) -> dict[str, tuple[float, str]]:
        """token -> (real settlement timestamp, where it came from).

        Tokens whose settlement moment is genuinely unknown are absent rather
        than defaulted. A caller that needs a settlement clock must decide
        what to do about not having one; it must not be handed our own row's
        insert time wearing the costume of a market event.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT token_id, settled_ts, settled_source FROM resolutions "
                "WHERE COALESCE(settled_ts,0) > 0").fetchall()
        return {str(r["token_id"]): (float(r["settled_ts"]),
                                     str(r["settled_source"] or ""))
                for r in rows}

    def backfill_settlement_times(self, limit: int = 5000) -> int:
        """Fill missing settlement moments from each market's last trade.

        For a settled market the final print is the closest observable proxy
        for when it ended, and it is a market fact rather than a fact about
        our polling. Recorded as `last_trade` so nobody mistakes it for the
        published settlement time — §6 asks for the real timestamp, and being
        explicit that this is an estimate is part of that.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT r.token_id, MAX(t.ts) AS last_ts
                     FROM resolutions r
                     JOIN wallet_trades t ON t.token_id = r.token_id
                    WHERE COALESCE(r.settled_ts,0) <= 0
                 GROUP BY r.token_id LIMIT ?""", (int(limit),)).fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE resolutions SET settled_ts=?, "
                    "settled_source='last_trade' WHERE token_id=?",
                    (float(row["last_ts"]), row["token_id"]))
            self._conn.commit()
        return len(rows)

    def markets_without_resolution(self, limit: int = 60) -> list[str]:
        """Markets wallets have traded whose settlement we do not know yet.

        Wallet skill is scored against settled outcomes, and the wallets worth
        ranking trade far beyond the tracked universe — so waiting for a market
        to appear in that universe before learning how it ended would leave
        almost every observed wallet permanently unscoreable, and the ranking
        permanently empty. Oldest activity first: those are the markets most
        likely to have settled by now.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT t.market_id, MAX(t.ts) AS last_ts
                     FROM wallet_trades t
                    WHERE t.market_id != ''
                      -- TRADES only. The /activity backfill records
                      -- redemptions for conditions the wallet never traded on
                      -- our tape, which are real events but are not episodes
                      -- and settle no research question. Counting them tripled
                      -- the apparent backlog with markets nothing depends on.
                      AND COALESCE(t.event_type, 'TRADE') = 'TRADE'
                      AND NOT EXISTS (SELECT 1 FROM resolutions r
                                       WHERE r.market_id = t.market_id)
                 GROUP BY t.market_id
                 ORDER BY last_ts ASC
                    LIMIT ?""", (int(limit),)).fetchall()
        return [r["market_id"] for r in rows]

    def resolutions(self) -> dict[str, float]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT token_id, price FROM resolutions").fetchall()
        return {r["token_id"]: r["price"] for r in rows}

    # -- reads ---------------------------------------------------------------

    def trades_since(self, since_ts: int,
                     market_id: str = "") -> list[dict]:
        sql = "SELECT * FROM wallet_trades WHERE ts >= ?"
        params: list[Any] = [int(since_ts)]
        if market_id:
            sql += " AND market_id = ?"
            params.append(market_id)
        sql += " ORDER BY ts"
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def trades_for_wallet(self, wallet: str, since_ts: int = 0,
                          limit: int = 5_000) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM wallet_trades WHERE wallet = ? AND ts >= ? "
                "ORDER BY ts DESC LIMIT ?",
                (wallet.lower(), int(since_ts), int(limit))).fetchall()
        return [dict(r) for r in reversed(rows)]

    def wallet_summaries(self, since_ts: int = 0) -> list[dict]:
        """One aggregate row per observed wallet — the feature layer's input.

        Aggregated in SQL rather than in Python because this runs against every
        wallet ever seen, every ranking pass, and the row count is unbounded by
        design.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT wallet,
                          COUNT(*)                    AS trades,
                          COUNT(DISTINCT market_id)   AS markets,
                          MIN(ts)                     AS first_seen,
                          MAX(ts)                     AS last_seen,
                          AVG(usdc)                   AS avg_usdc,
                          MAX(usdc)                   AS max_usdc,
                          SUM(usdc)                   AS gross_usdc
                     FROM wallet_trades
                    WHERE ts >= ?
                 GROUP BY wallet""", (int(since_ts),)).fetchall()
        return [dict(r) for r in rows]

    def market_activity(self, since_ts: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT market_id,
                          COUNT(*)                  AS trades,
                          COUNT(DISTINCT wallet)    AS wallets,
                          SUM(usdc)                 AS gross_usdc,
                          SUM(CASE WHEN side='BUY' THEN usdc ELSE -usdc END)
                                                    AS net_usdc
                     FROM wallet_trades
                    WHERE ts >= ? AND market_id != ''
                 GROUP BY market_id""", (int(since_ts),)).fetchall()
        return [dict(r) for r in rows]

    def market_baseline(self, market_id: str, before_hour: int,
                        hours: int = 72) -> list[float]:
        """Hourly USDC flow for a market, most recent first, excluding *now*.

        Reads the rollup table so the baseline is not truncated by raw-trade
        pruning, and excludes the current hour so a spike is never compared
        against a window that already contains it.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT gross_usdc FROM market_flow WHERE market_id = ? AND "
                "hour < ? ORDER BY hour DESC LIMIT ?",
                (str(market_id), int(before_hour), int(hours))).fetchall()
        return [float(r["gross_usdc"] or 0.0) for r in rows]

    def rollup(self, since_ts: int = 0) -> int:
        """Fold raw trades into hourly per-market buckets. Idempotent.

        Recomputes each touched bucket from the raw rows rather than adding to
        it, so running twice over the same window cannot double-count.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT market_id, CAST(ts / 3600 AS INTEGER) AS hour,
                          COUNT(*) AS trades, COUNT(DISTINCT wallet) AS wallets,
                          SUM(usdc) AS gross,
                          SUM(CASE WHEN side='BUY' THEN usdc ELSE -usdc END) AS net
                     FROM wallet_trades
                    WHERE ts >= ? AND market_id != ''
                 GROUP BY market_id, hour""", (int(since_ts),)).fetchall()
            if not rows:
                return 0
            self._conn.executemany(
                """INSERT INTO market_flow(market_id, hour, trades, wallets,
                                           gross_usdc, net_usdc)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(market_id, hour) DO UPDATE SET
                     trades=excluded.trades, wallets=excluded.wallets,
                     gross_usdc=excluded.gross_usdc,
                     net_usdc=excluded.net_usdc""",
                [(r["market_id"], r["hour"], r["trades"], r["wallets"],
                  float(r["gross"] or 0.0), float(r["net"] or 0.0))
                 for r in rows])
            self._conn.commit()
        return len(rows)

    # -- derived intel -------------------------------------------------------

    def save_scores(self, intel: Iterable[WalletIntel]) -> int:
        now = time.time()
        rows = [
            (w.wallet.lower(), w.label, w.rank, w.score, w.raw_score,
             w.confidence, w.sample, w.trades, w.markets, w.win_rate,
             w.avg_return, w.realized_usdc, w.avg_usdc, w.std_usdc, w.max_usdc,
             w.trades_per_day, w.hedge_rate, w.first_seen, w.last_seen,
             1 if w.in_cohort else 0, 1 if w.pinned else 0, now)
            for w in intel
        ]
        if not rows:
            return 0
        assignments = ", ".join(f"{f}=excluded.{f}" for f in _SCORE_FIELDS)
        with self._lock:
            self._conn.executemany(
                f"""INSERT INTO wallet_scores(
                      wallet, {', '.join(_SCORE_FIELDS)}, updated_ts)
                    VALUES({','.join('?' * (len(_SCORE_FIELDS) + 2))})
                    ON CONFLICT(wallet) DO UPDATE SET {assignments},
                      updated_ts=excluded.updated_ts""", rows)
            self._conn.commit()
        return len(rows)

    def load_scores(self) -> dict[str, WalletIntel]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM wallet_scores").fetchall()
        out: dict[str, WalletIntel] = {}
        for row in rows:
            data = dict(row)
            out[data["wallet"]] = WalletIntel(
                wallet=data["wallet"], label=data["label"] or "",
                rank=int(data["rank"] or 0), score=float(data["score"] or 0.0),
                raw_score=float(data["raw_score"] or 0.0),
                confidence=float(data["confidence"] or 0.0),
                sample=int(data["sample"] or 0),
                trades=int(data["trades"] or 0),
                markets=int(data["markets"] or 0),
                win_rate=float(data["win_rate"] or 0.0),
                avg_return=float(data["avg_return"] or 0.0),
                realized_usdc=float(data["realized_usdc"] or 0.0),
                avg_usdc=float(data["avg_usdc"] or 0.0),
                std_usdc=float(data["std_usdc"] or 0.0),
                max_usdc=float(data["max_usdc"] or 0.0),
                trades_per_day=float(data["trades_per_day"] or 0.0),
                hedge_rate=float(data["hedge_rate"] or 0.0),
                first_seen=int(data["first_seen"] or 0),
                last_seen=int(data["last_seen"] or 0),
                in_cohort=bool(data["in_cohort"]),
                pinned=bool(data["pinned"]),
            )
        return out

    def record_anomalies(self, anomalies: Iterable[AnomalySignal]) -> int:
        rows = [
            (a.ts, a.kind, a.subject, a.wallet, a.label, a.market_id,
             a.token_id, a.outcome, a.z, a.strength, _json(a.detail))
            for a in anomalies
        ]
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                """INSERT INTO anomalies(ts, kind, subject, wallet, label,
                     market_id, token_id, outcome, z, strength, detail)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""", rows)
            self._conn.commit()
        return len(rows)

    def recent_anomalies(self, limit: int = 100,
                         kind: str = "") -> list[dict]:
        sql = "SELECT * FROM anomalies"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def record_raw_events(self, kind: str,
                          rows: Iterable[tuple[float, str, str, dict]]) -> int:
        """Persist raw events verbatim. Rows are ``(ts, market, token, payload)``.

        The autoincrement id is the event_id: unique, ordered, and free.
        """
        prepared = [(float(ts), str(kind), str(market), str(token),
                     _json(payload))
                    for ts, market, token, payload in rows]
        if not prepared:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO raw_events(ts, kind, market_id, token_id, payload)"
                " VALUES(?,?,?,?,?)", prepared)
            self._conn.commit()
        return len(prepared)

    # -- research capture ----------------------------------------------------

    def record_research_rows(self, rows: Iterable[tuple]) -> int:
        """Persist feature vectors. Rows are ``(ts, token, market, outcome,
        category, features_dict)``."""
        prepared = [(float(ts), str(token), str(market), str(outcome),
                     str(category), _json(features))
                    for ts, token, market, outcome, category, features in rows]
        if not prepared:
            return 0
        with self._lock:
            self._conn.executemany(
                """INSERT INTO research_rows(ts, token_id, market_id, outcome,
                     category, features) VALUES(?,?,?,?,?,?)""", prepared)
            self._conn.commit()
        return len(prepared)

    def record_activity(self, rows: Iterable[tuple]) -> int:
        """Persist non-trade lifecycle events (REDEEM / MERGE / SPLIT).

        Same table as the trade tape, because one chronological record per
        wallet is the point — a redemption is part of the story of a
        condition, not a separate story. Same natural key too, so re-running
        the collector is free.
        """
        prepared = list(rows)
        if not prepared:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                """INSERT OR IGNORE INTO wallet_trades(
                     wallet, ts, market_id, token_id, outcome, side, price,
                     size, usdc, question, tx, source, event_type)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", prepared)
            self._conn.commit()
            return self._conn.total_changes - before

    def busiest_wallets(self, limit: int = 500,
                        min_trades: int = 20) -> list:
        """Wallets worth collecting activity for, busiest first.

        Busiest rather than random: a wallet with one trade has one condition,
        and a redemption for it changes one label. The wallets that move the
        research population are the ones with many conditions.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT wallet, COUNT(*) n FROM wallet_trades "
                "WHERE COALESCE(event_type,'TRADE')='TRADE' "
                "GROUP BY wallet HAVING n >= ? ORDER BY n DESC LIMIT ?",
                (int(min_trades), int(limit))).fetchall()
        return [str(r["wallet"]).lower() for r in rows]

    def redemptions(self) -> dict:
        """`(wallet, market_id) -> earliest redemption timestamp`.

        The shape the lifecycle layer needs: it asks "had this wallet already
        redeemed this condition before the prediction instant", which is a
        lookup, not a scan.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT wallet, market_id, MIN(ts) ts FROM wallet_trades "
                "WHERE event_type='REDEEM' AND market_id != '' "
                "GROUP BY wallet, market_id").fetchall()
        return {(str(r["wallet"]).lower(), str(r["market_id"])): float(r["ts"])
                for r in rows}

    def activity_census(self) -> dict:
        """How many of each event kind are on record."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT COALESCE(event_type,'TRADE') k, COUNT(*) n "
                "FROM wallet_trades GROUP BY k").fetchall()
        return {str(r["k"]): int(r["n"]) for r in rows}

    def research_tokens(self, min_rows: int = 200) -> list[dict]:
        """Tokens with enough captured history to research, richest first."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT token_id, market_id, outcome, category,
                          COUNT(*) AS rows, MIN(ts) AS first_ts, MAX(ts) AS last_ts
                     FROM research_rows
                 GROUP BY token_id
                   HAVING COUNT(*) >= ?
                 ORDER BY rows DESC""", (int(min_rows),)).fetchall()
        return [dict(r) for r in rows]

    def research_series(self, token_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, features FROM research_rows WHERE token_id = ? "
                "ORDER BY ts", (str(token_id),)).fetchall()
        out = []
        for row in rows:
            try:
                features = json.loads(row["features"])
            except (json.JSONDecodeError, TypeError):
                continue
            out.append({"ts": float(row["ts"]), **features})
        return out

    # -- cheap stats ---------------------------------------------------------

    def set_meta(self, key: str, value: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value, ts) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "ts=excluded.ts", (str(key), float(value), time.time()))
            self._conn.commit()

    def get_meta(self, key: str) -> Optional[float]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (str(key),)).fetchone()
        return float(row["value"]) if row else None

    # -- housekeeping --------------------------------------------------------

    def prune(self, max_age_days: float) -> int:
        """Drop raw trades past the horizon, after rolling them up.

        The rollup runs first and unconditionally: pruning raw rows that were
        never folded into a bucket would silently shorten every market's flow
        baseline, and the detector that reads it would go quiet rather than
        fail — the worst way for this to break.
        """
        if max_age_days <= 0:
            return 0
        self.rollup()
        cutoff = int(time.time() - max_age_days * 86400)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM wallet_trades WHERE ts < ?", (cutoff,))
            removed = cur.rowcount
            # Research rows are kept longer than raw trades, and deliberately:
            # discovery needs the longest series it can get, and one row per
            # token per capture tick is far cheaper to keep than the trade tape.
            self._conn.execute(
                "DELETE FROM research_rows WHERE ts < ?",
                (time.time() - max_age_days * 3 * 86400,))
            self._conn.execute(
                "DELETE FROM raw_events WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return removed

    def stats(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                """SELECT (SELECT COUNT(*) FROM wallet_trades)          AS trades,
                          (SELECT COUNT(DISTINCT wallet) FROM wallet_trades)
                                                                        AS wallets,
                          (SELECT COUNT(*) FROM wallet_scores WHERE rank > 0)
                                                                        AS ranked,
                          (SELECT COUNT(*) FROM anomalies)              AS anomalies,
                          (SELECT COUNT(*) FROM resolutions)            AS resolutions,
                          (SELECT COUNT(*) FROM research_rows)          AS research_rows,
                          (SELECT COUNT(DISTINCT token_id) FROM research_rows)
                                                                        AS research_tokens,
                          (SELECT MIN(ts) FROM wallet_trades)           AS oldest
                """).fetchone()
        return dict(row) if row else {}

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
