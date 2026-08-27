"""The durable V3 data layer.

One SQLite file under `var/`, WAL mode, opened read-write only here. The V1
databases under `Polymarket-Bot-DATA/` are opened read-only by
`ingest/source.py` and are never written to by V3 — same non-negotiable rule
V2 adopted, and `tests/test_v2_untouched.py` still asserts it.

Every table carries the four provenance columns the brief requires:

    ts              when the fact was true (epoch seconds)
    source          which collector or computation produced it
    data_version    bumped when the MEANING of a column changes
    schema_version  bumped when the SHAPE of a table changes

That combination is what makes a number on the dashboard auditable. Without
`source`, "wallet win rate 68%" is a claim; with it, it is a claim you can walk
back to a row.

Design note on `capture_ts` vs `ts`: collectors record both. `ts` is when the
world changed, `capture_ts` is when we found out. Backtests filter on
`capture_ts` — using `ts` would hand the backtest information that arrived
after the decision it is scoring, which is the news-leakage failure mode in
`docs/ANTI-OVERFITTING.md`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..config import DATA_VERSION, SCHEMA_VERSION, Settings

_LOCAL = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- ---------------------------------------------------------------- provenance
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, ts INTEGER NOT NULL);

-- Health/freshness per collector. The dashboard's SYSTEM tab is a SELECT here,
-- never a computed guess.
CREATE TABLE IF NOT EXISTS collector_health (
    collector      TEXT PRIMARY KEY,
    status         TEXT NOT NULL,            -- OK|STALE|ERROR|NOT_CONFIGURED|DISABLED
    last_success_ts INTEGER NOT NULL DEFAULT 0,
    last_attempt_ts INTEGER NOT NULL DEFAULT 0,
    rows_total     INTEGER NOT NULL DEFAULT 0,
    first_row_ts   INTEGER NOT NULL DEFAULT 0,
    last_row_ts    INTEGER NOT NULL DEFAULT 0,
    error          TEXT NOT NULL DEFAULT '',
    detail         TEXT NOT NULL DEFAULT '');

-- ------------------------------------------------------------------- markets
CREATE TABLE IF NOT EXISTS markets (
    market_id   TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL DEFAULT '',
    event_id    TEXT NOT NULL DEFAULT '',
    question    TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT '',
    outcomes    TEXT NOT NULL DEFAULT '[]',
    created_ts  INTEGER NOT NULL DEFAULT 0,
    close_ts    INTEGER NOT NULL DEFAULT 0,
    resolved_ts INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'UNKNOWN',
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_markets_event ON markets(event_id);
CREATE INDEX IF NOT EXISTS ix_markets_close ON markets(close_ts);

-- Order-book snapshots. EMPTY on a fresh install and expected to be: this data
-- cannot be backfilled, only accumulated. See docs/DATA-HONESTY.md.
CREATE TABLE IF NOT EXISTS book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    market_id TEXT NOT NULL DEFAULT '',
    best_bid REAL, best_ask REAL, mid REAL, spread REAL,
    bid_depth REAL, ask_depth REAL, imbalance REAL,
    levels TEXT NOT NULL DEFAULT '[]',       -- JSON [[px,sz],...] both sides
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_book_token_ts ON book_snapshots(token_id, ts);

-- --------------------------------------------------------------------- news
-- The three-timestamp rule: event_ts (it happened), ts (it was published),
-- capture_ts (we saw it). Collapsing these is how look-ahead enters a news
-- backtest.
CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid       TEXT UNIQUE,
    source_name TEXT NOT NULL DEFAULT '',
    source_class TEXT NOT NULL DEFAULT 'UNKNOWN',  -- OFFICIAL|WIRE|MEDIA|SOCIAL
    reliability REAL NOT NULL DEFAULT 0.0,         -- 0..1, configured not learned
    title     TEXT NOT NULL DEFAULT '',
    body      TEXT NOT NULL DEFAULT '',
    url       TEXT NOT NULL DEFAULT '',
    entities  TEXT NOT NULL DEFAULT '[]',
    topics    TEXT NOT NULL DEFAULT '[]',
    confirmation TEXT NOT NULL DEFAULT 'UNCONFIRMED',
                 -- RUMOR|UNCONFIRMED|MULTI_SOURCE|OFFICIAL|MARKET_PRICED
    event_ts  INTEGER NOT NULL DEFAULT 0,
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_news_capture ON news_items(capture_ts);

CREATE TABLE IF NOT EXISTS news_market_links (
    news_id INTEGER NOT NULL, market_id TEXT NOT NULL,
    relevance REAL NOT NULL DEFAULT 0.0,
    direction REAL NOT NULL DEFAULT 0.0,      -- -1..+1 implied for outcome YES
    magnitude REAL NOT NULL DEFAULT 0.0,
    method TEXT NOT NULL DEFAULT '',          -- how the link was inferred
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL,
    PRIMARY KEY (news_id, market_id));

-- ------------------------------------------------------- resolution timing
-- THE FIX for V1's highest-value data gap. `resolutions.settled_ts` is 0 in
-- all 8,116 rows of intel.sqlite3, so the moment an outcome became public is
-- recorded nowhere and point-in-time wallet track record is untestable.
--
-- V3 must not write to the V1 file, so the true settlement time lives here and
-- `source.iter_settled` prefers it. `method` records HOW each timestamp was
-- established, because a venue-reported resolution time and a first-observed
-- proxy are different qualities of evidence and must never be averaged.
CREATE TABLE IF NOT EXISTS resolution_times (
    token_id   TEXT PRIMARY KEY,
    market_id  TEXT NOT NULL DEFAULT '',
    condition_id TEXT NOT NULL DEFAULT '',
    outcome_price REAL,
    settled_ts INTEGER NOT NULL,
    method     TEXT NOT NULL,
               -- VENUE_REPORTED | CHAIN_EVENT | FIRST_OBSERVED | V1_FALLBACK
    confidence REAL NOT NULL DEFAULT 0.0,
    v1_ts      INTEGER NOT NULL DEFAULT 0,   -- what V1 had, for comparison
    delta_secs INTEGER NOT NULL DEFAULT 0,   -- settled_ts - v1_ts
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_restime_method ON resolution_times(method);

-- ---------------------------------------------------------------- blockchain
CREATE TABLE IF NOT EXISTS chain_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_hash TEXT NOT NULL, block_number INTEGER NOT NULL DEFAULT 0,
    log_index INTEGER NOT NULL DEFAULT 0,
    wallet TEXT NOT NULL DEFAULT '', counterparty TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',            -- TRANSFER|APPROVAL|SPLIT|MERGE|REDEEM
    asset TEXT NOT NULL DEFAULT '', amount REAL NOT NULL DEFAULT 0.0,
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL,
    UNIQUE(tx_hash, log_index));
CREATE INDEX IF NOT EXISTS ix_chain_wallet_ts ON chain_events(wallet, ts);

-- ------------------------------------------------------------------ research
CREATE TABLE IF NOT EXISTS hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    family     TEXT NOT NULL DEFAULT '',
    statement  TEXT NOT NULL DEFAULT '',
    params     TEXT NOT NULL DEFAULT '{}',
    tested     INTEGER NOT NULL DEFAULT 0,
    p_value    REAL,
    effect     REAL,
    n          INTEGER NOT NULL DEFAULT 0,
    outcome    TEXT NOT NULL DEFAULT 'UNTESTED',
    pass_id    TEXT NOT NULL DEFAULT '',
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_hyp_pass ON hypotheses(pass_id);

-- The denominator. Every sweep records how many transformations it evaluated,
-- so a p-value can never be quoted without the search that produced it.
CREATE TABLE IF NOT EXISTS research_passes (
    pass_id TEXT PRIMARY KEY,
    started_ts INTEGER NOT NULL, finished_ts INTEGER NOT NULL DEFAULT 0,
    tested INTEGER NOT NULL DEFAULT 0,
    distinct_tested INTEGER NOT NULL DEFAULT 0,
    surviving INTEGER NOT NULL DEFAULT 0,
    bh_alpha REAL NOT NULL DEFAULT 0.1,
    bh_threshold REAL NOT NULL DEFAULT 0.0,
    detail TEXT NOT NULL DEFAULT '{}',
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS strategies (
    strategy_id TEXT NOT NULL, version INTEGER NOT NULL,
    parent_strategy TEXT NOT NULL DEFAULT '',
    family TEXT NOT NULL DEFAULT '',
    features TEXT NOT NULL DEFAULT '[]',
    params TEXT NOT NULL DEFAULT '{}',
    train_from INTEGER NOT NULL DEFAULT 0, train_to INTEGER NOT NULL DEFAULT 0,
    valid_from INTEGER NOT NULL DEFAULT 0, valid_to INTEGER NOT NULL DEFAULT 0,
    oos_from INTEGER NOT NULL DEFAULT 0, oos_to INTEGER NOT NULL DEFAULT 0,
    trade_count INTEGER NOT NULL DEFAULT 0,
    win_rate REAL NOT NULL DEFAULT 0.0,
    expectancy REAL NOT NULL DEFAULT 0.0,
    profit_factor REAL NOT NULL DEFAULT 0.0,
    max_drawdown REAL NOT NULL DEFAULT 0.0,
    evidence_quality TEXT NOT NULL DEFAULT 'UNRATED',
    failure_modes TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'DISCOVERED',
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL,
    PRIMARY KEY (strategy_id, version));
CREATE INDEX IF NOT EXISTS ix_strat_status ON strategies(status);

-- ------------------------------------------------------------------- agents
CREATE TABLE IF NOT EXISTS agent_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, agent TEXT NOT NULL, subject TEXT NOT NULL,
    stance TEXT NOT NULL DEFAULT 'ABSTAIN',   -- FOR|AGAINST|ABSTAIN
    confidence REAL NOT NULL DEFAULT 0.0,
    probability REAL,
    thesis TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    objections TEXT NOT NULL DEFAULT '[]',
    inputs_used TEXT NOT NULL DEFAULT '[]',
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_agent_run ON agent_outputs(run_id);
CREATE INDEX IF NOT EXISTS ix_agent_name ON agent_outputs(agent, ts);

-- ---------------------------------------------------------------- decisions
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL DEFAULT '',
    market_id TEXT NOT NULL DEFAULT '', token_id TEXT NOT NULL DEFAULT '',
    strategy_id TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'RESEARCH',
    action TEXT NOT NULL,                     -- TRADE|DO_NOT_TRADE
    side TEXT NOT NULL DEFAULT '',
    signal_price REAL, fair_probability REAL, market_probability REAL,
    edge REAL, confidence REAL,
    size_usdc REAL NOT NULL DEFAULT 0.0, size_shares REAL NOT NULL DEFAULT 0.0,
    expected_value REAL NOT NULL DEFAULT 0.0,
    max_loss REAL NOT NULL DEFAULT 0.0,
    gates TEXT NOT NULL DEFAULT '{}',         -- every gate + verdict + reason
    blocking_gate TEXT NOT NULL DEFAULT '',
    reasons_for TEXT NOT NULL DEFAULT '[]',
    reasons_against TEXT NOT NULL DEFAULT '[]',
    evidence_ref TEXT NOT NULL DEFAULT '',
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_dec_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS ix_dec_action ON decisions(action, ts);

-- Paper and live fills share one table; `mode` separates them. Keeping them
-- apart in two tables is how a reporting bug silently counts paper PnL as real.
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL, market_id TEXT NOT NULL DEFAULT '',
    token_id TEXT NOT NULL DEFAULT '', side TEXT NOT NULL DEFAULT 'BUY',
    signal_price REAL NOT NULL DEFAULT 0.0,
    expected_fill REAL NOT NULL DEFAULT 0.0,
    actual_fill REAL NOT NULL DEFAULT 0.0,
    slippage REAL NOT NULL DEFAULT 0.0, latency_ms INTEGER NOT NULL DEFAULT 0,
    market_impact REAL NOT NULL DEFAULT 0.0,
    size_usdc REAL NOT NULL DEFAULT 0.0, size_shares REAL NOT NULL DEFAULT 0.0,
    fees REAL NOT NULL DEFAULT 0.0,
    uncertainty TEXT NOT NULL DEFAULT '[]',
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_fill_mode ON fills(mode, ts);

CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY, mode TEXT NOT NULL,
    market_id TEXT NOT NULL DEFAULT '', token_id TEXT NOT NULL DEFAULT '',
    strategy_id TEXT NOT NULL DEFAULT '', wallet_followed TEXT NOT NULL DEFAULT '',
    opened_ts INTEGER NOT NULL, closed_ts INTEGER NOT NULL DEFAULT 0,
    entry_price REAL NOT NULL DEFAULT 0.0, exit_price REAL,
    size_usdc REAL NOT NULL DEFAULT 0.0, size_shares REAL NOT NULL DEFAULT 0.0,
    realized_pnl REAL NOT NULL DEFAULT 0.0,
    unrealized_pnl REAL NOT NULL DEFAULT 0.0,
    resolution REAL, status TEXT NOT NULL DEFAULT 'OPEN',
    correlation_key TEXT NOT NULL DEFAULT '',
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_pos_status ON positions(mode, status);

-- ------------------------------------------------------------------ learning
CREATE TABLE IF NOT EXISTS loss_forensics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT NOT NULL, strategy_id TEXT NOT NULL DEFAULT '',
    classification TEXT NOT NULL DEFAULT 'unknown',
    predictable INTEGER NOT NULL DEFAULT 0,
    failed_agent TEXT NOT NULL DEFAULT '', failed_feature TEXT NOT NULL DEFAULT '',
    predicted REAL, actual REAL,
    narrative TEXT NOT NULL DEFAULT '',
    remedy TEXT NOT NULL DEFAULT '',           -- parameter|feature|risk|retire|none
    applied INTEGER NOT NULL DEFAULT 0,
    oos_improved INTEGER,
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS missed_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL, token_id TEXT NOT NULL DEFAULT '',
    decision_id TEXT NOT NULL DEFAULT '',
    would_have_returned REAL NOT NULL DEFAULT 0.0,
    rejection_gate TEXT NOT NULL DEFAULT '',
    rejection_correct INTEGER,
    executable INTEGER, liquidity_sufficient INTEGER,
    signal_visible_at_time INTEGER,
    exploited_by_wallet TEXT NOT NULL DEFAULT '',
    narrative TEXT NOT NULL DEFAULT '',
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS counterfactuals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL, variant TEXT NOT NULL,
    pnl REAL NOT NULL DEFAULT 0.0, note TEXT NOT NULL DEFAULT '',
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'INFO',
    subject TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
    acked INTEGER NOT NULL DEFAULT 0,
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_alert_ts ON alerts(ts);

-- §40. Things the system noticed on its own, ranked by importance x expected
-- economic impact x urgency. `surfaced` records whether it cleared the floor
-- and was shown; everything else is kept anyway, so "you never told me" has an
-- answer either way. The three factors are ESTIMATES used to order a queue —
-- nothing downstream of this table reads them.
CREATE TABLE IF NOT EXISTS discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'SYSTEM',
    headline TEXT NOT NULL DEFAULT '', measured TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0.0, impact REAL NOT NULL DEFAULT 0.0,
    urgency REAL NOT NULL DEFAULT 0.0, priority REAL NOT NULL DEFAULT 0.0,
    why TEXT NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT '',
    surfaced INTEGER NOT NULL DEFAULT 0, acked INTEGER NOT NULL DEFAULT 0,
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_disc_key ON discoveries(key);
CREATE INDEX IF NOT EXISTS ix_disc_pri ON discoveries(priority);

-- §29. What a supplied document was read as: the classified statements, the
-- candidates they map to, and the concepts they rest on that this system has
-- no column for. Kept so a claim can be traced back to the page it came from.
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
    chars INTEGER NOT NULL DEFAULT 0, words INTEGER NOT NULL DEFAULT 0,
    claims TEXT NOT NULL DEFAULT '[]', proposals TEXT NOT NULL DEFAULT '[]',
    missing_data TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '',
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);

-- §6/§31. Every tool call the autonomous engineer made, with its arguments
-- and its result. This is how "what did the AI change" is answered from the
-- store rather than from anyone's recollection, and it is the audit trail that
-- makes handing an agent write access to the project reviewable after the fact.
CREATE TABLE IF NOT EXISTS agent_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool TEXT NOT NULL, args TEXT NOT NULL DEFAULT '{}',
    ok INTEGER NOT NULL DEFAULT 1, result TEXT NOT NULL DEFAULT '',
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    checkpoint_id TEXT NOT NULL DEFAULT '',
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_agent_action_ts ON agent_actions(ts);

CREATE TABLE IF NOT EXISTS agent_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    objective TEXT NOT NULL, finished INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '', answer TEXT NOT NULL DEFAULT '',
    steps INTEGER NOT NULL DEFAULT 0,
    files_changed TEXT NOT NULL DEFAULT '[]',
    checkpoint_id TEXT NOT NULL DEFAULT '',
    tests_passed INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '', elapsed_ms INTEGER NOT NULL DEFAULT 0,
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);

-- §31. A checkpoint is a JOIN of the git commit and the store state at one
-- instant, plus the objective a human stated for the work that followed. Git
-- already versions the code better than a bespoke table could; what it cannot
-- record is which strategies were live and how many rows each table held, and
-- restoring one half without the other is a new configuration, not a rollback.
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_id TEXT NOT NULL UNIQUE, label TEXT NOT NULL DEFAULT '',
    objective TEXT NOT NULL DEFAULT '', git_sha TEXT NOT NULL DEFAULT '',
    git_branch TEXT NOT NULL DEFAULT '', git_dirty INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '{}', mode TEXT NOT NULL DEFAULT '',
    live_authorized INTEGER NOT NULL DEFAULT 0,
    tests TEXT NOT NULL DEFAULT '', rollback TEXT NOT NULL DEFAULT '',
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);

-- Control-console transcript. §22 (research memory) applied to the chat
-- interface: every turn is kept with the mode it was read as, the evidence
-- that answered it and whether a command was run, so "why did it say that"
-- has an answer that does not depend on anybody's recollection.
CREATE TABLE IF NOT EXISTS console_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'RESEARCH',
    state TEXT NOT NULL DEFAULT 'RESEARCH', topics TEXT NOT NULL DEFAULT '[]',
    finding TEXT NOT NULL DEFAULT '[]', diagnosis TEXT NOT NULL DEFAULT '[]',
    actions TEXT NOT NULL DEFAULT '[]', ran TEXT NOT NULL DEFAULT '',
    llm_available INTEGER NOT NULL DEFAULT 0,
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_console_ts ON console_turns(ts);

-- Human authorisations. LIVE mode is a row here, never a config value.
CREATE TABLE IF NOT EXISTS authorizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL, granted INTEGER NOT NULL,
    actor TEXT NOT NULL DEFAULT 'human', note TEXT NOT NULL DEFAULT '',
    snapshot TEXT NOT NULL DEFAULT '{}',
    ts INTEGER NOT NULL, capture_ts INTEGER NOT NULL, source TEXT NOT NULL,
    data_version INTEGER NOT NULL, schema_version INTEGER NOT NULL);
"""


class Store:
    """Thread-local connections over one SQLite file.

    Thread-local rather than a shared connection because the HTTP server, the
    collectors and the research loop all read concurrently; SQLite objects are
    not safe to share across threads and a single lock would serialise the
    dashboard behind a research sweep.
    """

    def __init__(self, st: Settings) -> None:
        self.st = st
        self.path: Path = st.store_db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    # -- connection ---------------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        """One connection per (thread, database path).

        Keyed by path, not just by thread. A bare `_LOCAL.conn` would hand the
        first store's connection to every later store in the same thread —
        harmless with a single store in production, and silently catastrophic
        anywhere two stores coexist, which is every test run.
        """
        cache = getattr(_LOCAL, "conns", None)
        if cache is None:
            cache = _LOCAL.conns = {}
        key = str(self.path)
        c = cache.get(key)
        if c is None:
            c = sqlite3.connect(key, timeout=30.0, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=30000")
            cache[key] = c
        return c

    def _init(self) -> None:
        c = self.conn()
        c.executescript(SCHEMA)
        c.commit()
        cur = c.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
        if row is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            self.set_meta("created_ts", str(int(time.time())))
        elif int(row[0]) != SCHEMA_VERSION:
            # Migrations are additive-only in V3; a shape change gets a new
            # table name rather than an in-place ALTER, so a half-applied
            # migration can never silently reinterpret existing rows.
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            self.set_meta(f"migrated_from_{row[0]}_ts", str(int(time.time())))

    # -- meta ---------------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        c = self.conn()
        c.execute("INSERT INTO meta(key,value,ts) VALUES(?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                  "ts=excluded.ts", (key, value, int(time.time())))
        c.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn().execute("SELECT value FROM meta WHERE key=?",
                                  (key,)).fetchone()
        return row[0] if row else default

    # -- generic write ------------------------------------------------------
    def insert(self, table: str, rows: Iterable[dict], *, source: str,
               replace: bool = False) -> int:
        """Insert with provenance stamped automatically.

        Callers supply the domain columns; `ts`, `capture_ts`, `source`,
        `data_version` and `schema_version` are filled here so no call site can
        forget them. A row without provenance is a row nobody can audit.
        """
        rows = list(rows)
        if not rows:
            return 0
        now = int(time.time())
        prepared = []
        for r in rows:
            d = dict(r)
            d.setdefault("ts", now)
            d.setdefault("capture_ts", now)
            d["source"] = source
            d["data_version"] = DATA_VERSION
            d["schema_version"] = SCHEMA_VERSION
            for k, v in list(d.items()):
                if isinstance(v, (dict, list, tuple)):
                    d[k] = json.dumps(v, separators=(",", ":"), default=str)
                elif isinstance(v, bool):
                    d[k] = int(v)
            prepared.append(d)

        # Rows are grouped by their exact key set and written one group per
        # statement. Two wrong approaches this avoids:
        #
        #   * first row's keys      — silently drops a column a later row
        #                             supplies and the first omits
        #   * union of all keys     — binds NULL for absent columns, and a NULL
        #                             against a NOT NULL DEFAULT is a constraint
        #                             violation that `INSERT OR IGNORE` swallows,
        #                             so the row vanishes with no error
        #
        # Grouping lets every omitted column fall through to its schema default,
        # which is what the caller meant by omitting it.
        groups: dict = {}
        for d in prepared:
            groups.setdefault(tuple(sorted(d.keys())), []).append(d)

        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        c = self.conn()
        written = 0
        for cols, rows_g in groups.items():
            sql = (f"{verb} INTO {table} ({','.join(cols)}) "
                   f"VALUES ({','.join('?' * len(cols))})")
            cur = c.executemany(sql, [[r[k] for k in cols] for r in rows_g])
            written += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
        # `rowcount` reflects rows ACTUALLY written, so a batch deduplicated by
        # a UNIQUE constraint reports what landed rather than what was offered.
        return written

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn().execute(sql, params).fetchall()]

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        r = self.conn().execute(sql, params).fetchone()
        return dict(r) if r else None

    def scalar(self, sql: str, params: Sequence[Any] = (), default=0):
        r = self.conn().execute(sql, params).fetchone()
        return default if r is None or r[0] is None else r[0]

    def count(self, table: str, where: str = "", params: Sequence[Any] = ()) -> int:
        sql = f"SELECT COUNT(*) FROM {table}" + (f" WHERE {where}" if where else "")
        return int(self.scalar(sql, params))

    # -- health -------------------------------------------------------------
    def record_health(self, collector: str, status: str, *, error: str = "",
                      rows_total: int | None = None, detail: str = "",
                      success: bool = False) -> None:
        now = int(time.time())
        c = self.conn()
        existing = self.one("SELECT * FROM collector_health WHERE collector=?",
                            (collector,))
        last_success = now if success else (
            existing["last_success_ts"] if existing else 0)
        c.execute(
            "INSERT INTO collector_health(collector,status,last_success_ts,"
            "last_attempt_ts,rows_total,error,detail) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(collector) DO UPDATE SET status=excluded.status,"
            "last_success_ts=excluded.last_success_ts,"
            "last_attempt_ts=excluded.last_attempt_ts,"
            "rows_total=COALESCE(excluded.rows_total,collector_health.rows_total),"
            "error=excluded.error, detail=excluded.detail",
            (collector, status, last_success, now,
             rows_total if rows_total is not None else 0, error, detail))
        c.commit()

    def health(self) -> list[dict]:
        return self.query("SELECT * FROM collector_health ORDER BY collector")

    def history_span_days(self, table: str, col: str = "ts") -> float:
        lo = self.scalar(f"SELECT MIN({col}) FROM {table}", default=0)
        hi = self.scalar(f"SELECT MAX({col}) FROM {table}", default=0)
        return round(max(0, int(hi) - int(lo)) / 86400.0, 2) if lo and hi else 0.0

    def alert(self, kind: str, message: str, *, severity: str = "INFO",
              subject: str = "", source: str = "engine") -> None:
        self.insert("alerts", [{"kind": kind, "severity": severity,
                                "subject": subject, "message": message}],
                    source=source)

    def close(self) -> None:
        cache = getattr(_LOCAL, "conns", None) or {}
        c = cache.pop(str(self.path), None)
        if c is not None:
            c.close()
