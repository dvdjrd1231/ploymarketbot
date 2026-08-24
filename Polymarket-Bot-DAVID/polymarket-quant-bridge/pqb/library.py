"""
The persistent strategy library: a growing scientific record, never a reset.

The operator's core fix, in his words: the hourly pass must mean *"take
everything we have learned so far, incorporate the newest data, search for
additional relationships, test them rigorously, update existing strategies,
and improve the persistent library"* — never *"forget everything and find the
best strategies right now."* Before this module, every discovery pass
overwrote the board; a strategy that survived validation vanished the moment
the next pass ran. That is a search process amnesia loop, and it is gone.

Three tables, one discipline:

* ``strategies`` — one row per strategy VERSION, forever. A permanent id
  (rule-family signature + version), the frozen rule, its status, and its
  cumulative out-of-sample record. Retirement changes ``status``; nothing is
  ever deleted, so future analysis can ask why something failed and under
  which regimes.
* ``validations`` — one row per strategy per INDEPENDENT MARKET, appended by
  frozen replays. UNIQUE(strategy, market): re-testing the market that
  already testified adds no independent evidence, so cumulative counts can
  never be inflated by repetition. 2 markets / 10 trades genuinely grows to
  50 markets / 2,000 trades, and never resets.
* ``passes`` — one row per strategy per discovery pass (the recent-form
  record), which is what makes degradation GRADUAL: validated -> watch ->
  degraded -> retired takes repeated bad evidence, not one cold streak.

Versioning: identity is the rule FAMILY (direction + features + operators,
thresholds excluded). A new threshold set for a known family becomes a new
VERSION with its own permanent row and its own record — so an "optimization"
must prove itself against unseen data before anyone believes it improved
anything.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    id            TEXT PRIMARY KEY,     -- signature#vN
    signature     TEXT NOT NULL,
    version       INTEGER NOT NULL,
    rule          TEXT NOT NULL,        -- frozen; never mutated
    rule_hash     TEXT NOT NULL,
    describe      TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    created_ts    REAL NOT NULL,
    updated_ts    REAL NOT NULL,
    last_validated_ts REAL DEFAULT 0,
    retired_reason TEXT DEFAULT '',
    in_score      REAL DEFAULT 0,       -- in-sample, for reference only
    in_win        REAL DEFAULT 0,
    in_sharpe     REAL DEFAULT 0,
    -- Every market whose data informed this rule's discovery or any
    -- re-discovery. Permanently INVALID as validation evidence: a live
    -- market's lastTs keeps extending, so yesterday's discovery market can
    -- drift into tomorrow's newest-30% holdout — without this list, a rule
    -- could be "validated" on data that suggested it.
    discovery_markets TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_strat_sig ON strategies(signature);
CREATE INDEX IF NOT EXISTS idx_strat_status ON strategies(status);

-- One row per strategy per INDEPENDENT market. The uniqueness is the honesty:
-- evidence grows by covering NEW markets, never by re-counting old ones.
CREATE TABLE IF NOT EXISTS validations (
    strategy_id   TEXT NOT NULL,
    market_id     TEXT NOT NULL,
    ts            REAL NOT NULL,
    period        TEXT DEFAULT '',
    trades        INTEGER DEFAULT 0,
    wins          INTEGER DEFAULT 0,
    pnl           REAL DEFAULT 0,
    drawdown      REAL DEFAULT 0,
    PRIMARY KEY (strategy_id, market_id)
);

-- One row per strategy per discovery pass: the recent-form record that makes
-- degradation gradual and evidence-based rather than one-bad-day.
CREATE TABLE IF NOT EXISTS passes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id   TEXT NOT NULL,
    ts            REAL NOT NULL,
    trades        INTEGER DEFAULT 0,
    wins          INTEGER DEFAULT 0,
    pnl           REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_passes_strat ON passes(strategy_id, ts);

-- One row per strategy per market ATTEMPTED, whether or not the attempt
-- produced anything. This table exists because of a specific and expensive
-- confusion: a replay that generates zero trades was being written into
-- `validations`, and `excluded_markets` reads that table, so the market was
-- permanently spent. The audit counted 114 of 681 validation rows as
-- zero-trade burns, with some candidates consuming most of the available
-- market supply while producing no evidence at all.
--
-- A rule that did not fire in a market has not been tested there. That is a
-- NON-OBSERVATION, and the correct accounting is: record that we tried, do
-- not consume the market, and do not let the candidate retry it forever.
-- Attempts are aggregated per (strategy, market) — the retry count, first and
-- last timestamps, the last reason and the accumulated compute cost answer
-- every question the per-attempt log would, without growing without bound.
CREATE TABLE IF NOT EXISTS attempts (
    strategy_id   TEXT NOT NULL,
    market_id     TEXT NOT NULL,
    first_ts      REAL NOT NULL,
    last_ts       REAL NOT NULL,
    tries         INTEGER DEFAULT 1,
    result        TEXT NOT NULL DEFAULT 'no_trades',  -- evidence|no_trades|error
    trades        INTEGER DEFAULT 0,
    reason        TEXT DEFAULT '',
    stage         TEXT DEFAULT '',
    exc_type      TEXT DEFAULT '',
    cost_seconds  REAL DEFAULT 0,
    PRIMARY KEY (strategy_id, market_id)
);
CREATE INDEX IF NOT EXISTS idx_attempt_result ON attempts(result);

-- Facts about the LIBRARY rather than about any strategy in it. The one that
-- matters today is the sizing epoch: the moment research started sizing to
-- the real account. Evidence recorded before it was measured on a different
-- account and is not comparable with evidence recorded after it, so the
-- boundary has to be a stored date rather than a thing everyone remembers.
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL,
    ts     REAL NOT NULL
);
"""


# Meta key for the moment research began sizing to the real account.
SIZING_EPOCH_KEY = "sizing_epoch_ts"

# Meta key recording that legacy zero-trade `validations` rows have been
# migrated into `attempts` and their markets released.
BURN_MIGRATION_KEY = "zero_trade_burns_migrated"

# How many times a candidate may attempt the same market without producing a
# single trade before that pair is parked. A frozen replay over an immutable
# settled series is deterministic, so a second try is only worth spending
# because the underlying series can be REBUILT — a widened backfill or a
# changed construction recipe genuinely changes the answer. Beyond that it is
# the same computation for the same result, and §26 asks for more independent
# information per unit of compute, not more repetitions of a known nothing.
MAX_NO_TRADE_TRIES = 2

# Results an attempt can have. `evidence` is the only one that also writes to
# `validations`; the other two consume compute and nothing else.
ATTEMPT_EVIDENCE = "evidence"
ATTEMPT_NO_TRADES = "no_trades"
ATTEMPT_ERROR = "error"


def _rule_hash(rule: dict) -> str:
    return hashlib.sha1(
        json.dumps(rule, sort_keys=True, default=str).encode()).hexdigest()[:12]


class StrategyLibrary:
    """SQLite-backed, append-only knowledge of every strategy ever found."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            cols = {r[1] for r in
                    self._conn.execute("PRAGMA table_info(strategies)")}
            if "discovery_markets" not in cols:   # pre-existing libraries
                self._conn.execute("ALTER TABLE strategies ADD COLUMN "
                                   "discovery_markets TEXT DEFAULT '[]'")
            if "family" not in cols:              # pre-family libraries
                self._conn.execute("ALTER TABLE strategies ADD COLUMN "
                                   "family TEXT DEFAULT ''")
            # How many times a verdict has been re-opened, and when last.
            # A rejection that keeps being re-tried and keeps failing is a
            # settled question; without a count there is nothing to stop a
            # widened re-open rule from cycling the same rows every pass.
            if "reopen_count" not in cols:
                self._conn.execute("ALTER TABLE strategies ADD COLUMN "
                                   "reopen_count INTEGER DEFAULT 0")
            if "last_reopen_ts" not in cols:
                self._conn.execute("ALTER TABLE strategies ADD COLUMN "
                                   "last_reopen_ts REAL DEFAULT 0")
            # Where this market sat relative to the candidate's discovery:
            # forward | concurrent | historical | unknown. Stored on the
            # evidence row rather than recomputed, because the classification
            # depends on when the candidate was discovered and what the pool
            # looked like THEN — recomputing it later against a moved clock
            # would quietly reclassify settled evidence.
            # Which research pathway proposed this candidate (§11). Distinct
            # from the rule TYPE, which says how it replays: a sequence chain
            # and the inverse of a sequence chain replay identically and are
            # not the same source.
            if "source" not in cols:
                self._conn.execute("ALTER TABLE strategies ADD COLUMN "
                                   "source TEXT DEFAULT ''")
            if "parent_id" not in cols:
                self._conn.execute("ALTER TABLE strategies ADD COLUMN "
                                   "parent_id TEXT DEFAULT ''")
            vcols = {r[1] for r in
                     self._conn.execute("PRAGMA table_info(validations)")}
            if "temporal_class" not in vcols:
                self._conn.execute("ALTER TABLE validations ADD COLUMN "
                                   "temporal_class TEXT DEFAULT ''")
            self._conn.commit()
        self._migrate_zero_trade_burns()

    # -- migration -----------------------------------------------------------

    def _migrate_zero_trade_burns(self) -> int:
        """Move legacy zero-trade `validations` rows into `attempts`, and give
        their markets back.

        Runs once, idempotently, on any library that predates the split. It is
        a release, not a deletion: every burn is preserved as an attempt with
        its original timestamp, so the historical record of what was tried is
        intact — the only thing that changes is that those markets stop being
        counted as spent. `cumulative()` already filtered `trades > 0`, so no
        evidence figure moves; the markets simply become available again.
        """
        with self._lock:
            done = self._conn.execute(
                "SELECT value FROM meta WHERE key=?",
                (BURN_MIGRATION_KEY,)).fetchone()
            if done:
                return 0
            burns = self._conn.execute(
                "SELECT strategy_id, market_id, ts FROM validations "
                "WHERE trades <= 0").fetchall()
            for row in burns:
                self._conn.execute(
                    "INSERT OR IGNORE INTO attempts(strategy_id, market_id, "
                    "first_ts, last_ts, tries, result, trades, reason) "
                    "VALUES(?,?,?,?,1,?,0,?)",
                    (row["strategy_id"], row["market_id"], float(row["ts"]),
                     float(row["ts"]), ATTEMPT_NO_TRADES,
                     "migrated from a zero-trade validation row"))
            self._conn.execute("DELETE FROM validations WHERE trades <= 0")
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value, ts) VALUES(?,?,?)",
                (BURN_MIGRATION_KEY, str(len(burns)), time.time()))
            self._conn.commit()
        return len(burns)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- intake --------------------------------------------------------------

    def upsert_candidate(self, signature: str, rule: dict, describe: str,
                         in_score: float = 0.0, in_win: float = 0.0,
                         in_sharpe: float = 0.0,
                         discovery_markets: Iterable[str] = (),
                         family: str = "", source: str = "",
                         parent_id: str = "") -> str:
        """Register a discovered rule; returns its permanent id.

        Same family + same thresholds -> the existing version (touched, never
        reset). Same family + NEW thresholds -> a NEW VERSION with its own
        empty record: an optimisation earns trust from zero, on unseen data.

        ``discovery_markets`` — the markets this pass searched to (re)find
        the rule. They join the version's permanent exclusion list: on a
        re-discovery the list WIDENS, it never narrows, because being picked
        again means those markets' data vouched for the rule in-sample.
        """
        now = time.time()
        rh = _rule_hash(rule)
        if not source:
            from .sources import source_of
            source = source_of(rule)
        with self._lock:
            row = self._conn.execute(
                "SELECT id, discovery_markets FROM strategies WHERE "
                "signature=? AND rule_hash=?", (signature, rh)).fetchone()
            if row:
                try:
                    known = set(json.loads(row["discovery_markets"] or "[]"))
                except (TypeError, ValueError):
                    known = set()
                known.update(str(m) for m in discovery_markets)
                self._conn.execute(
                    "UPDATE strategies SET updated_ts=?, "
                    "in_score=MAX(in_score,?), discovery_markets=?, "
                    # Pre-family rows heal on re-discovery; a stored
                    # family is never overwritten.
                    "family=CASE WHEN family='' THEN ? ELSE family END, "
                    "source=CASE WHEN COALESCE(source,'')='' THEN ? "
                    "ELSE source END "
                    "WHERE id=?",
                    (now, float(in_score), json.dumps(sorted(known)),
                     str(family), str(source), row["id"]))
                self._conn.commit()
                return row["id"]
            version = 1 + (self._conn.execute(
                "SELECT COALESCE(MAX(version),0) v FROM strategies "
                "WHERE signature=?", (signature,)).fetchone()["v"])
            new_id = f"{signature}#v{version}"
            self._conn.execute(
                "INSERT INTO strategies(id, signature, version, rule, "
                "rule_hash, describe, status, created_ts, updated_ts, "
                "in_score, in_win, in_sharpe, discovery_markets, family, "
                "source, parent_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, signature, version, json.dumps(rule, default=str),
                 rh, describe, "new", now, now, float(in_score),
                 float(in_win), float(in_sharpe),
                 json.dumps(sorted({str(m) for m in discovery_markets})),
                 str(family), str(source), str(parent_id)))
            self._conn.commit()
            return new_id

    def record_attempt(self, strategy_id: str, market_id: str, result: str,
                       trades: int = 0, reason: str = "", stage: str = "",
                       exc_type: str = "", cost_seconds: float = 0.0) -> int:
        """Record that a replay was ATTEMPTED. Returns the new try count.

        Every replay calls this, including the ones that succeed — the attempt
        ledger is the complete record of what compute was spent and what came
        back. Only `result == ATTEMPT_EVIDENCE` is additionally written to
        `validations`, and only that consumes the market.
        """
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT tries FROM attempts WHERE strategy_id=? AND "
                "market_id=?", (strategy_id, str(market_id))).fetchone()
            tries = int(row["tries"]) + 1 if row else 1
            if row:
                self._conn.execute(
                    "UPDATE attempts SET last_ts=?, tries=?, result=?, "
                    "trades=?, reason=?, stage=?, exc_type=?, "
                    "cost_seconds=cost_seconds+? WHERE strategy_id=? "
                    "AND market_id=?",
                    (now, tries, result, int(trades), reason, stage, exc_type,
                     float(cost_seconds), strategy_id, str(market_id)))
            else:
                self._conn.execute(
                    "INSERT INTO attempts(strategy_id, market_id, first_ts, "
                    "last_ts, tries, result, trades, reason, stage, exc_type, "
                    "cost_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (strategy_id, str(market_id), now, now, 1, result,
                     int(trades), reason, stage, exc_type,
                     float(cost_seconds)))
            self._conn.commit()
        return tries

    def record_validation(self, strategy_id: str, market_id: str,
                          trades: int, wins: int, pnl: float,
                          drawdown: float, period: str = "",
                          temporal_class: str = "") -> bool:
        """Append one independent market's frozen-replay evidence.

        Returns False when this market already testified for this strategy —
        the row is ignored and cumulative counts stay honest.

        A zero-trade replay is refused outright. It is not evidence and it
        must not consume the market: the caller records it as an attempt
        instead. The guard is here rather than only at the call site because
        this is the invariant the whole breadth accounting rests on, and it
        should be impossible to violate from anywhere.
        """
        if int(trades) <= 0:
            raise ValueError(
                "record_validation refused a zero-trade row for "
                f"{strategy_id} on market {market_id}: a rule that did not "
                "fire has not been tested there. Record it with "
                "record_attempt(result=ATTEMPT_NO_TRADES).")
        with self._lock:
            before = self._conn.total_changes
            self._conn.execute(
                "INSERT OR IGNORE INTO validations(strategy_id, market_id, "
                "ts, period, trades, wins, pnl, drawdown, temporal_class) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (strategy_id, str(market_id), time.time(), period,
                 int(trades), int(wins), float(pnl), float(drawdown),
                 str(temporal_class or "")))
            fresh = self._conn.total_changes > before
            if fresh:
                self._conn.execute(
                    "UPDATE strategies SET last_validated_ts=?, updated_ts=? "
                    "WHERE id=?", (time.time(), time.time(), strategy_id))
            self._conn.commit()
        return fresh

    def record_pass(self, strategy_id: str, trades: int, wins: int,
                    pnl: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO passes(strategy_id, ts, trades, wins, pnl) "
                "VALUES(?,?,?,?,?)",
                (strategy_id, time.time(), int(trades), int(wins),
                 float(pnl)))
            self._conn.commit()

    def set_status(self, strategy_id: str, status: str,
                   reason: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE strategies SET status=?, retired_reason=?, "
                "updated_ts=? WHERE id=?",
                (status, reason, time.time(), strategy_id))
            self._conn.commit()

    # -- reads ---------------------------------------------------------------

    def cumulative(self, strategy_id: str) -> dict:
        """The whole record: every independent market, summed, never reset.

        ``top_share`` is the P&L-concentration measure the operator
        specified: the largest single market's share of total positive
        P&L. A record whose profit is one market's story is fragile
        however good its headline expectancy looks.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) markets, COALESCE(SUM(trades),0) trades, "
                "COALESCE(SUM(wins),0) wins, COALESCE(SUM(pnl),0) pnl, "
                "COALESCE(MAX(drawdown),0) drawdown, "
                "MIN(period) p0, MAX(period) p1 "
                "FROM validations WHERE strategy_id=? AND trades > 0",
                (strategy_id,)).fetchone()
            per_market = self._conn.execute(
                "SELECT pnl FROM validations WHERE strategy_id=? "
                "AND trades > 0", (strategy_id,)).fetchall()
            by_class = self._conn.execute(
                "SELECT COALESCE(NULLIF(temporal_class,''),'unknown') k, "
                "COUNT(*) n, COALESCE(SUM(trades),0) t FROM validations "
                "WHERE strategy_id=? AND trades > 0 GROUP BY k",
                (strategy_id,)).fetchall()
        trades = int(row["trades"] or 0)
        positives = [float(r["pnl"]) for r in per_market
                     if float(r["pnl"]) > 0]
        top_share = (max(positives) / sum(positives)) if positives else 0.0
        # p0/p1 are themselves "start .. end" strings; span them, don't
        # concatenate them ("a .. b .. a .. b" is not a period).
        p0, p1 = row["p0"] or "", row["p1"] or ""
        start = p0.split(" .. ")[0] if p0 else ""
        end = p1.split(" .. ")[-1] if p1 else ""
        period = (f"{start} .. {end}" if start and end and start != end
                  else start or end)
        return {
            "markets": int(row["markets"] or 0), "trades": trades,
            "wins": int(row["wins"] or 0), "pnl": float(row["pnl"] or 0.0),
            "drawdown": float(row["drawdown"] or 0.0),
            "expectancy": (float(row["pnl"] or 0.0) / trades) if trades else 0.0,
            "win_rate": (int(row["wins"] or 0) / trades) if trades else 0.0,
            "period": period,
            "top_share": round(top_share, 4),
            # Breadth, split by where each market sat relative to discovery.
            # All of it is independent evidence — the rule was fitted to none
            # of it — but only `forward` is walk-forward evidence, and the
            # two must never be reported as the same thing (§4).
            "forward_markets": sum(int(r["n"]) for r in by_class
                                   if r["k"] == "forward"),
            "forward_trades": sum(int(r["t"]) for r in by_class
                                  if r["k"] == "forward"),
            "markets_by_temporal_class": {str(r["k"]): int(r["n"])
                                          for r in by_class},
        }

    def discovery_markets(self, strategy_id: str) -> set[str]:
        """The permanent contamination list: markets whose data informed this
        rule's discovery. A rule is never proven on the data that suggested
        it, so these can never testify, at any try count."""
        with self._lock:
            row = self._conn.execute(
                "SELECT discovery_markets FROM strategies WHERE id=?",
                (strategy_id,)).fetchone()
        try:
            return (set(json.loads(row["discovery_markets"] or "[]"))
                    if row else set())
        except (TypeError, ValueError):
            return set()

    def parked_markets(self, strategy_id: str) -> set[str]:
        """Markets this candidate has attempted `MAX_NO_TRADE_TRIES` times
        without ever producing a trade, plus those that keep erroring.

        Parked is not spent. These markets carry NO evidence, count toward no
        breadth figure, and are released the moment the candidate produces a
        trade there. They are skipped only so a rule that never fires stops
        re-buying the same nothing every pass.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT market_id FROM attempts WHERE strategy_id=? "
                "AND result != ? AND tries >= ?",
                (strategy_id, ATTEMPT_EVIDENCE, MAX_NO_TRADE_TRIES)).fetchall()
        return {str(r["market_id"]) for r in rows}

    def excluded_markets(self, strategy_id: str) -> set[str]:
        """Markets that may NOT serve as validation evidence right now.

        Three distinct reasons, deliberately kept distinct: the rule's own
        discovery markets (permanently contaminated), markets that have
        already testified (a market never testifies twice), and markets parked
        after repeated non-observations (temporary, and carrying no evidence).

        Note what is NOT in here any more: a market where the rule simply did
        not fire. That used to land in `validations` with zero trades and be
        excluded forever, which is how candidates spent the market supply
        without ever generating evidence.
        """
        return (self.discovery_markets(strategy_id)
                | self.evidence_markets(strategy_id)
                | self.parked_markets(strategy_id))

    def evidence_markets(self, strategy_id: str) -> set[str]:
        """Markets that actually testified — the breadth denominator. Only
        evidence-bearing markets are in here, by construction."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT market_id FROM validations WHERE strategy_id=? "
                "AND trades > 0", (strategy_id,)).fetchall()
        return {str(r["market_id"]) for r in rows}

    def attempt_ledger(self, strategy_id: str) -> list[dict]:
        """Every market this candidate has spent compute on, with what came
        back. §24: the answer to 'why has this been tested nowhere?' is here,
        and it distinguishes never-allocated from allocated-and-never-fired
        from allocated-and-crashed."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT market_id, first_ts, last_ts, tries, result, trades, "
                "reason, stage, exc_type, cost_seconds FROM attempts "
                "WHERE strategy_id=? ORDER BY last_ts", (strategy_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def attempt_summaries(self) -> dict[str, dict]:
        """Per-candidate attempt counts for the whole library, in one query.

        One query rather than one per candidate: the view rebuild already
        walks every strategy, and §26 is explicit that the answer is more
        information per unit of compute, not more compute.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT strategy_id, result, COUNT(*) pairs, "
                "COALESCE(SUM(tries),0) tries FROM attempts "
                "GROUP BY strategy_id, result").fetchall()
        out: dict[str, dict] = {}
        for row in rows:
            entry = out.setdefault(str(row["strategy_id"]),
                                   {"tries": 0, "evidence": 0,
                                    "zeroTrades": 0, "errors": 0})
            entry["tries"] += int(row["tries"])
            if row["result"] == ATTEMPT_EVIDENCE:
                entry["evidence"] += int(row["pairs"])
            elif row["result"] == ATTEMPT_ERROR:
                entry["errors"] += int(row["pairs"])
            else:
                entry["zeroTrades"] += int(row["pairs"])
        return out

    def attempt_health(self) -> dict:
        """The OOS HEALTH block (§23), straight from persisted attempts."""
        with self._lock:
            totals = self._conn.execute(
                "SELECT COUNT(*) pairs, COALESCE(SUM(tries),0) tries, "
                "COALESCE(SUM(cost_seconds),0) cost FROM attempts"
            ).fetchone()
            by_result = self._conn.execute(
                "SELECT result, COUNT(*) n FROM attempts GROUP BY result"
            ).fetchall()
            retryable = self._conn.execute(
                "SELECT COUNT(*) n FROM attempts WHERE result != ? "
                "AND tries < ?", (ATTEMPT_EVIDENCE, MAX_NO_TRADE_TRIES)
            ).fetchone()["n"]
            per_candidate = self._conn.execute(
                "SELECT s.id, COUNT(v.market_id) markets FROM strategies s "
                "LEFT JOIN validations v ON v.strategy_id = s.id "
                "AND v.trades > 0 GROUP BY s.id").fetchall()
        counts = {str(r["result"]): int(r["n"]) for r in by_result}
        breadths = sorted(int(r["markets"]) for r in per_candidate)
        n = len(breadths)
        return {
            "attemptPairs": int(totals["pairs"] or 0),
            "attemptTries": int(totals["tries"] or 0),
            "attemptCostSeconds": round(float(totals["cost"] or 0.0), 2),
            "evidenceAttempts": counts.get(ATTEMPT_EVIDENCE, 0),
            "zeroTradeAttempts": counts.get(ATTEMPT_NO_TRADES, 0),
            "errorAttempts": counts.get(ATTEMPT_ERROR, 0),
            "retryableMarkets": int(retryable),
            "candidates": n,
            "candidatesWithZeroOosMarkets": sum(1 for b in breadths if b == 0),
            "avgOosMarketsPerCandidate": (round(sum(breadths) / n, 2)
                                          if n else 0.0),
            "medianOosMarketsPerCandidate": (breadths[n // 2] if n else 0),
            "maxOosMarketsPerCandidate": (breadths[-1] if n else 0),
        }

    def market_ledger(self, strategy_id: str) -> list[dict]:
        """The candidate's complete evidence ledger, market by market, in
        the order it was earned — what makes every validation decision
        reconstructible from persisted data (the operator's §14)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT market_id, ts, period, trades, wins, pnl, drawdown "
                "FROM validations WHERE strategy_id=? ORDER BY ts",
                (strategy_id,)).fetchall()
        return [dict(r) for r in rows]

    def evidence_ledgers(self) -> dict[str, list[dict]]:
        """EVERY candidate's evidence ledger, in one query, keyed by id.

        `market_ledger` per candidate is the right shape for one candidate and
        the wrong shape for two thousand: the motif layer needs the market ids
        behind every row in the library at once, and asking for them one
        candidate at a time turns a single scan into two thousand round trips
        on the research pass's hot path.

        Read-only, and returns market IDS rather than counts on purpose. A
        caller given counts cannot tell whether two candidates were measured
        on the same market, which is the one thing the motif layer must never
        get wrong.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT strategy_id, market_id, ts, period, trades, wins, "
                "pnl, drawdown FROM validations ORDER BY ts").fetchall()
        out: dict[str, list[dict]] = {}
        for row in rows:
            out.setdefault(str(row["strategy_id"]), []).append(dict(row))
        return out

    def version_counts(self) -> dict[str, int]:
        """signature -> how many versions exist. One query, for the same
        reason `evidence_ledgers` exists."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT signature, COUNT(*) n FROM strategies "
                "GROUP BY signature").fetchall()
        return {str(r["signature"]): int(r["n"]) for r in rows}

    def periods_count(self, strategy_id: str) -> int:
        """Independent TIME periods (months) this strategy's evidence spans.

        The operator's §8: trades, markets, and time periods are three
        different kinds of independence. Reported and used for research
        priority; the validation gates are unchanged."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts FROM validations WHERE strategy_id=? "
                "AND trades > 0", (strategy_id,)).fetchall()
        months = {time.strftime("%Y-%m", time.gmtime(float(r["ts"])))
                  for r in rows}
        return len(months)

    def tested_markets(self, strategy_id: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT market_id FROM validations WHERE strategy_id=?",
                (strategy_id,)).fetchall()
        return {str(r["market_id"]) for r in rows}

    def recent_passes(self, strategy_id: str, n: int = 2) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT trades, wins, pnl, ts FROM passes "
                "WHERE strategy_id=? ORDER BY ts DESC LIMIT ?",
                (strategy_id, int(n))).fetchall()
        return [dict(r) for r in rows]

    def all_strategies(self, include_retired: bool = True) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM strategies ORDER BY updated_ts DESC").fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            if not include_retired and entry["status"] in ("retired",
                                                           "rejected"):
                continue
            try:
                entry["rule"] = json.loads(entry["rule"])
            except (TypeError, ValueError):
                entry["rule"] = {}
            out.append(entry)
        return out

    def evaluable(self) -> list[dict]:
        """Everything still worth challenging with new unseen data —
        which is every state except the terminal ones.

        `quarantined` is terminal for allocation purposes: spending replay
        budget on a rule whose features are constant in the validation data
        buys a guaranteed zero-trade result. The row stays in the library with
        its full history and its reason, exactly as §28 requires.
        """
        return [s for s in self.all_strategies()
                if s["status"] not in ("retired", "rejected", "quarantined")]

    def family_cumulative(self, signature: str) -> dict:
        """The HYPOTHESIS ledger: evidence for a signature across ALL its
        versions, one contribution per independent market.

        The operator's fragmentation fix: v1 tested on markets A,B and v3
        tested on markets B,C is FOUR version-rows but THREE independent
        market events for the underlying hypothesis. Market B counts once
        — its FIRST-recorded testimony (chronological, so no picking the
        version that happened to score best). Version ledgers are
        untouched; this is a second, read-only view above them.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT v.market_id, v.trades, v.wins, v.pnl, v.drawdown, "
                "v.ts FROM validations v JOIN strategies s "
                "ON s.id = v.strategy_id WHERE s.signature = ? "
                "AND v.trades > 0 ORDER BY v.ts", (signature,)).fetchall()
            versions = self._conn.execute(
                "SELECT COUNT(*) n FROM strategies WHERE signature = ?",
                (signature,)).fetchone()["n"]
        first_per_market: dict[str, Any] = {}
        for row in rows:
            first_per_market.setdefault(str(row["market_id"]), row)
        trades = sum(int(r["trades"]) for r in first_per_market.values())
        wins = sum(int(r["wins"]) for r in first_per_market.values())
        pnl = sum(float(r["pnl"]) for r in first_per_market.values())
        positives = [float(r["pnl"]) for r in first_per_market.values()
                     if float(r["pnl"]) > 0]
        return {
            "signature": signature,
            "versions": int(versions),
            "markets": len(first_per_market),
            "trades": trades, "wins": wins, "pnl": round(pnl, 6),
            "expectancy": round(pnl / trades, 6) if trades else 0.0,
            "win_rate": round(wins / trades, 4) if trades else 0.0,
            "top_share": round(max(positives) / sum(positives), 4)
            if positives else 0.0,
        }

    def family_metrics(self) -> list[dict]:
        """Per-hypothesis research metrics (§11) — interpretation only,
        never a promotion path. Versions stay the atomic records."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT signature, COUNT(*) versions, "
                "MIN(created_ts) first_seen, MAX(updated_ts) last_seen "
                "FROM strategies GROUP BY signature").fetchall()
        out = []
        for row in rows:
            ledger = self.family_cumulative(str(row["signature"]))
            per_version = []
            with self._lock:
                ids = self._conn.execute(
                    "SELECT id, status FROM strategies WHERE signature=?",
                    (str(row["signature"]),)).fetchall()
            for entry in ids:
                cumulative = self.cumulative(str(entry["id"]))
                if cumulative["trades"]:
                    per_version.append((str(entry["id"]),
                                        cumulative["expectancy"]))
            per_version.sort(key=lambda kv: kv[1])
            ledger.update({
                "firstSeen": float(row["first_seen"] or 0.0),
                "lastSeen": float(row["last_seen"] or 0.0),
                "statuses": {str(e["status"]) for e in ids},
                "worstVersion": per_version[0][0] if per_version else "",
                "bestVersion": per_version[-1][0] if per_version else "",
                "medianVersion": per_version[len(per_version) // 2][0]
                if per_version else "",
            })
            out.append(ledger)
        out.sort(key=lambda f: -f["expectancy"])
        return out

    # -- meta ----------------------------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value, ts) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "ts=excluded.ts", (key, str(value), time.time()))
            self._conn.commit()

    def sizing_epoch(self) -> float:
        """When research started sizing to the real account. 0 = never set.

        Validation evidence recorded before this moment was measured on a
        different account and cannot be compared with, or summed alongside,
        evidence recorded after it.
        """
        try:
            return float(self.get_meta(SIZING_EPOCH_KEY, "0") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def evidence_since(self, strategy_id: str, since_ts: float) -> int:
        """Independent markets that testified at or after ``since_ts``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) n FROM validations WHERE strategy_id=? "
                "AND ts >= ?", (strategy_id, float(since_ts))).fetchone()
        return int(row["n"] or 0)

    # -- re-opening historical verdicts ---------------------------------------

    def reopen_rejections(self, max_reopens: int = 3) -> dict[str, int]:
        """Re-open rejections that today's rules would not have made.

        Two things put a rejection back into validation:

        * **Breadth.** It was recorded on ONE market's evidence, which
          today's ``next_status`` would never accept as failure — one
          market's story can neither validate nor reject.
        * **Sizing.** Its evidence was measured before the sizing epoch, on
          an account a hundred times larger than the real one. A rule
          rejected for losing $11,001 on a notional $10,000 book lost $110
          on the real one; the verdict was returned on numbers that never
          described this account. Nothing about that rule has been shown.

        The second is why this had to widen. The breadth rule alone matched
        nothing in the delivered library — every one of its 146 rejections
        already carried two or more markets — so the re-open ran every pass
        and moved nothing, and the whole rejected pile stayed discarded on
        arithmetic that was wrong by two orders of magnitude.

        **The churn guard** is that a re-open must be justified by evidence
        the strategy does not yet have. A sizing re-open needs the record to
        be entirely pre-epoch: once a candidate has been re-challenged on
        correctly-sized data, its next rejection is a real one and this will
        not touch it again. ``max_reopens`` is the backstop underneath that
        — a hard ceiling on how many times any one verdict can be revisited,
        so no future rule can put a strategy into a cycle.

        Returns counts by reason. Safe to call every pass.
        """
        epoch = self.sizing_epoch()
        counts = {"breadth": 0, "sizing": 0, "held_by_guard": 0}

        for row in self.all_strategies():
            if row.get("status") != "rejected":
                continue
            strategy_id = row["id"]

            if int(row.get("reopen_count") or 0) >= int(max_reopens):
                counts["held_by_guard"] += 1
                continue

            cumulative = self.cumulative(strategy_id)

            if cumulative["trades"] > 0 and cumulative["markets"] < 2:
                key = "breadth"
                reason = ("re-opened: rejected on single-market evidence "
                          "under pre-breadth rules; awaiting independent "
                          "markets")
            elif (epoch > 0 and cumulative["trades"] > 0
                    and self.evidence_since(strategy_id, epoch) == 0):
                key = "sizing"
                reason = ("re-opened: every rejection figure was measured "
                          "at the pre-fix account size and describes no "
                          "real account; re-challenging on correctly-sized "
                          "holdout markets")
            else:
                continue

            self._reopen(strategy_id, reason)
            counts[key] += 1

        return counts

    def _reopen(self, strategy_id: str, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE strategies SET status='validating', "
                "retired_reason=?, updated_ts=?, "
                "reopen_count=COALESCE(reopen_count,0)+1, last_reopen_ts=? "
                "WHERE id=?",
                (reason, time.time(), time.time(), strategy_id))
            self._conn.commit()

    def reopen_single_market_rejections(self) -> int:
        """The breadth half of :meth:`reopen_rejections`, kept as its own
        entry point. Idempotent: a re-opened row is no longer rejected, so
        a second call changes nothing."""
        reopened = 0
        for row in self.all_strategies():
            if row.get("status") != "rejected":
                continue
            cumulative = self.cumulative(row["id"])
            if cumulative["trades"] > 0 and cumulative["markets"] < 2:
                self._reopen(
                    row["id"],
                    "re-opened: rejected on single-market evidence under "
                    "pre-breadth rules; awaiting independent markets")
                reopened += 1
        return reopened

    def evidence_summary(self) -> dict:
        """What the library currently holds, for a before/after report."""
        with self._lock:
            validations = self._conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(trades),0) trades, "
                "COALESCE(SUM(pnl),0) pnl, MIN(ts) t0, MAX(ts) t1 "
                "FROM validations").fetchone()
            passes = self._conn.execute(
                "SELECT COUNT(*) n FROM passes").fetchone()
            statuses = self._conn.execute(
                "SELECT status, COUNT(*) n FROM strategies "
                "GROUP BY status").fetchall()
        return {
            "validations": int(validations["n"] or 0),
            "trades": int(validations["trades"] or 0),
            "pnl": float(validations["pnl"] or 0.0),
            "first_ts": float(validations["t0"] or 0.0),
            "last_ts": float(validations["t1"] or 0.0),
            "passes": int(passes["n"] or 0),
            "statuses": {str(r["status"]): int(r["n"]) for r in statuses},
        }

    def reset_for_resizing(self, note: str = "") -> dict:
        """Void every verdict measured at the wrong account size. Destructive.

        Fixing the sizing does not fix the RECORD. Cumulative evidence is
        summed across every market a strategy ever testified on and is never
        reset by design — that permanence is the point of the library — so
        rows booked at ~100x the real position size go on being added to
        correctly-sized rows forever. A strategy carrying -$11,001 of
        historical loss cannot climb back to a positive cumulative
        expectancy on $100-scale evidence within any realistic number of
        passes, so nothing would ever promote and the fix would show no
        visible effect at all.

        Rescaling the rows instead of deleting them was considered and does
        not work. The old figures are not a constant multiple of the right
        ones: the ladder resolves per market from that market's own price,
        the account drawdown halt is a dollar figure derived from starting
        equity (so a run could be truncated at one size and not the other),
        and the cost model itself changed — a flat fee that used to be
        charged per contract is now charged per fill. There is no divisor
        that turns the old numbers into the new ones.

        So the evidence is cleared and has to be re-earned against the same
        frozen holdout pool, which is cheap: the pool is cached and
        immutable, and replaying it is what the hourly pass already does.
        Strategies, rules and discovery exclusions are all KEPT — this
        voids verdicts, not knowledge. The sizing epoch is stamped so
        everything afterwards can tell the two eras apart.

        Returns a before/after summary. Take a file copy first; this cannot
        be undone from inside the library.
        """
        before = self.evidence_summary()
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM validations")
            self._conn.execute("DELETE FROM passes")
            # Every status was a conclusion drawn from what was just
            # deleted, including the rejections. A candidate with no
            # evidence is 'new' — which is exactly what next_status returns
            # for a zero-trade record, so the ladder stays self-consistent.
            self._conn.execute(
                "UPDATE strategies SET status='new', retired_reason=?, "
                "last_validated_ts=0, reopen_count=0, last_reopen_ts=0, "
                "updated_ts=? WHERE status != 'new'",
                ("evidence voided: recorded before the sizing fix", now))
            self._conn.commit()
        self.set_meta(SIZING_EPOCH_KEY, repr(now))
        if note:
            self.set_meta("sizing_epoch_note", note)
        after = self.evidence_summary()
        return {"before": before, "after": after, "epoch": now}

    def rejected_for_recheck(self, limit: int = 8,
                             min_seconds: float = 0.0) -> list[dict]:
        """Rejections due another swing at unseen data, oldest verdict first.

        A rejection is a verdict on the evidence available when it was made,
        not a proof about the rule, and the holdout pool grows every pass.
        Rotating a bounded number of rejected candidates back through the
        replay each pass is what stops "rejected" from meaning "deleted"
        without letting 146 dead rules eat a budget the live candidates
        need. ``next_status`` still requires the whole cumulative record to
        turn positive before any of them re-enters validation, so this
        widens the *search*, never the *standard*.
        """
        cutoff = time.time() - float(min_seconds)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM strategies WHERE status='rejected' "
                "AND updated_ts <= ? ORDER BY updated_ts ASC LIMIT ?",
                (cutoff, int(limit))).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            try:
                entry["rule"] = json.loads(entry["rule"])
            except (TypeError, ValueError):
                entry["rule"] = {}
            out.append(entry)
        return out

    def family_stats(self) -> dict[str, dict[str, int]]:
        """Per-family status counts — what lets discovery spend effort where
        evidence is promising and stop re-registering variants of an idea
        the unseen data keeps refusing (the operator's adaptive search)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT family, status, COUNT(*) n FROM strategies "
                "WHERE family != '' GROUP BY family, status").fetchall()
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            out.setdefault(str(row["family"]), {})[str(row["status"])] = \
                int(row["n"])
        return out


# -- the status machine ------------------------------------------------------

def next_status(current: str, cumulative: dict, last_pass: Optional[dict],
                cfg) -> tuple[str, str]:
    """One evidence-based step of the ladder. Returns (status, reason).

    Promotion needs cumulative evidence; demotion needs REPEATED recent
    failure — gradual by construction, exactly as specified: a validated
    strategy hitting one bad pass goes to WATCH, not to the bin, and only
    sustained deterioration retires it. Retired is terminal — that rule had
    its full chance and degraded through the whole ladder; revival would be
    a new version earning trust from zero. Rejected is different: kept with
    its reason, and allowed back ONLY if enough NEW unseen-market evidence
    flips the whole cumulative record positive — then it re-enters at
    validating, one step, never straight back to trading.
    """
    if current == "retired":
        return current, ""

    trades = cumulative["trades"]
    markets = cumulative["markets"]
    expectancy = cumulative["expectancy"]

    if current == "rejected":
        if trades >= cfg.oos_min_trades and markets >= cfg.oos_min_markets \
                and expectancy > cfg.oos_min_expectancy:
            return "validating", (
                f"cumulative record recovered to {expectancy:+.4f} over "
                f"{trades} unseen trades - re-entering validation")
        return current, ""

    recent_bad = bool(last_pass and last_pass.get("trades", 0) >= 10
                      and last_pass.get("pnl", 0.0) < 0)

    if trades == 0:
        return "new", ""
    # A first showing that is decisively negative is rejected outright —
    # but rejection needs BREADTH just as promotion does (the operator's
    # OOS-breadth rule): one market's 30 losing trades is one market's
    # story. A single-market loser stays validating, waits for unseen
    # markets, and earns near-zero research priority meanwhile.
    if current in ("new", "validating") and trades >= cfg.oos_min_trades \
            and markets >= 2 and expectancy < 0:
        return "rejected", (f"negative expectancy {expectancy:.4f} over "
                            f"{trades} unseen trades across "
                            f"{markets} markets")
    if trades < cfg.oos_min_trades or markets < cfg.oos_min_markets:
        return "validating", ""

    # Demotion path: repeated recent failure, one step at a time.
    if recent_bad:
        ladder = {"high_confidence": "watch", "validated": "watch",
                  "watch": "degraded", "degraded": "retired"}
        step = ladder.get(current)
        if step:
            reason = ("sustained deterioration across passes"
                      if step == "retired" else
                      f"losing pass ({last_pass.get('pnl', 0.0):+.2f} over "
                      f"{last_pass.get('trades', 0)} trades)")
            return step, reason
    elif current in ("watch", "degraded") and expectancy > cfg.oos_min_expectancy:
        # Recovery is also gradual: one good pass climbs one step.
        return {"watch": "validated", "degraded": "watch"}[current], ""

    if expectancy <= cfg.oos_min_expectancy:
        return "degraded", (f"cumulative expectancy {expectancy:.4f} at or "
                            "below the floor")
    # Market-independence gate (the operator's concentration test): a
    # record whose positive P&L is mostly ONE market's story cannot be
    # promoted to tradable, however good the headline expectancy. It stays
    # validating until broader evidence dilutes the concentration.
    top_share = float(cumulative.get("top_share", 0.0))
    ceiling = float(getattr(cfg, "oos_max_concentration", 0.7))
    if top_share > ceiling and current not in ("watch", "degraded"):
        return "validating", (f"profit concentrated: top market carries "
                              f"{top_share:.0%} of positive P&L "
                              f"(ceiling {ceiling:.0%})")
    if trades >= cfg.oos_hc_trades and markets >= cfg.oos_hc_markets \
            and current in ("validated", "high_confidence"):
        return "high_confidence", ""
    if current in ("watch", "degraded", "high_confidence"):
        return current, ""
    return "validated", ""


def maturity_of(status: str, cumulative: dict, cfg) -> str:
    """Research-only evidence-maturity state (§9) layered OVER the existing
    status ladder — the ladder itself is untouched, and every promotion
    still runs through next_status exactly as before.

    The distinction that matters most (§8): a version with 1-7 unseen
    trades is INSUFFICIENT_EVIDENCE, not a statistically meaningful
    failure — and not a success either."""
    if status in ("retired",):
        return "RETIRED"
    if status == "rejected":
        return "REJECTED"
    if status in ("degraded", "watch"):
        return "ROBUSTNESS_TESTING"
    if status in ("validated", "high_confidence"):
        return "VALIDATED"
    trades = int(cumulative.get("trades") or 0)
    expectancy = float(cumulative.get("expectancy") or 0.0)
    if trades == 0:
        return "DISCOVERED"
    if trades < 8:
        # NEAR_MISS (operator's §24): too little evidence to conclude,
        # but what exists points the right way — worth prioritized
        # research, never tradable from this state.
        return "NEAR_MISS" if expectancy > 0 else "INSUFFICIENT_EVIDENCE"
    if trades < cfg.oos_min_trades:
        return ("NEAR_MISS" if expectancy > 0
                else "EVIDENCE_ACCUMULATING")
    return "SUFFICIENT_SAMPLE_FOR_REVIEW"


def blocking_of(status: str, cumulative: dict, cfg) -> str:
    """The 'why not validated?' diagnostic (§18): the CURRENT blocking
    condition, named — never a bare 'validating'. Empty when nothing
    blocks (validated and beyond, or terminal states with their own
    stored reasons)."""
    if status in ("validated", "high_confidence", "retired", "rejected"):
        return ""
    trades = int(cumulative.get("trades") or 0)
    markets = int(cumulative.get("markets") or 0)
    expectancy = float(cumulative.get("expectancy") or 0.0)
    top_share = float(cumulative.get("top_share") or 0.0)
    if trades == 0:
        return "NO_OOS_EVENTS_YET"
    if trades < cfg.oos_min_trades:
        return f"INSUFFICIENT_OOS_EVENTS ({trades}/{cfg.oos_min_trades})"
    if markets < cfg.oos_min_markets:
        return f"INSUFFICIENT_MARKETS ({markets}/{cfg.oos_min_markets})"
    if expectancy <= cfg.oos_min_expectancy:
        return f"NEGATIVE_NET_EXPECTANCY ({expectancy:+.4f})"
    ceiling = float(getattr(cfg, "oos_max_concentration", 0.7))
    if top_share > ceiling:
        return f"EVENT_CONCENTRATION (top market {top_share:.0%})"
    if status in ("watch", "degraded"):
        return "RECENT_DETERIORATION"
    return ""


def overfit_risk(in_sample_win: float, cumulative: dict) -> float:
    """In-sample/OOS divergence (§12), 0..1. Diagnostic only.

    83% in-sample collapsing to 10% out-of-sample is the fingerprint of a
    fitted artifact; the measure only speaks once the OOS sample can
    (>=10 trades), so a thin sample is never called overfit."""
    trades = int(cumulative.get("trades") or 0)
    if trades < 10 or in_sample_win <= 0:
        return 0.0
    oos_win = float(cumulative.get("win_rate") or 0.0)
    return round(max(0.0, min(1.0, in_sample_win - oos_win)), 4)


def research_priority(cumulative: dict, status: str, in_sample_win: float,
                      periods: int, cfg,
                      family_weight: float = 1.0) -> float:
    """Where the next holdout evaluation slots should go (§7, §23).

    Uses ONLY evidence already collected at the current stage — never a
    future outcome. Favors positive OOS expectancy, market breadth,
    temporal diversity, and diversification; punishes negative OOS
    records and in-sample/OOS divergence. Purely an allocation signal:
    it cannot promote, and it cannot trade.
    """
    if status in ("retired", "rejected"):
        return 0.0
    trades = int(cumulative.get("trades") or 0)
    if trades == 0:
        return round(0.5 * family_weight, 4)   # untested: worth a first look
    expectancy = float(cumulative.get("expectancy") or 0.0)
    if expectancy <= 0:
        # A negative unseen record earns fewer resources however pretty
        # the in-sample story is — the operator's explicit rule.
        base = 0.15
    else:
        need = max(1, cfg.oos_min_trades)
        base = 0.5 + 0.3 * min(1.0, trades / need)
        base += 0.1 * min(1.0, int(cumulative.get("markets") or 0)
                          / max(1, cfg.oos_min_markets))
        base += 0.1 * min(1.0, periods / 3.0)
        base -= 0.2 * float(cumulative.get("top_share") or 0.0)
    base -= 0.3 * overfit_risk(in_sample_win, cumulative)
    return round(max(0.0, min(1.5, base * family_weight)), 4)


def meta_family_weights(metrics: list[dict]) -> dict[str, float]:
    """The meta-discovery layer (§21): which hypothesis families have
    historically survived unseen data? Families whose versions mostly
    carry positive OOS records get a modest research-priority bonus;
    families the data keeps refusing get a modest malus. Bounded to
    x0.8..x1.2 so meta-learning steers effort — it never decides."""
    out: dict[str, float] = {}
    for family in metrics:
        trades = int(family.get("trades") or 0)
        if trades < 10:
            out[str(family.get("signature") or "")] = 1.0
            continue
        expectancy = float(family.get("expectancy") or 0.0)
        out[str(family.get("signature") or "")] = \
            1.2 if expectancy > 0 else 0.8
    return out


def blockers_of(status: str, cumulative: dict, cfg,
                attempts: Optional[dict] = None) -> list[str]:
    """EVERY active blocker with its numeric target (§9 of the master
    fix), machine-readable — 'INSUFFICIENT...' explains nothing, and one
    truncated word was hiding four different bottlenecks. Empty when
    nothing blocks or the state is terminal.

    `attempts` is this candidate's row from `attempt_summaries()`. It splits
    the single old "no OOS events yet" into the three genuinely different
    situations behind it: never allocated any market, allocated and the rule
    never fired, allocated and the replay crashed. The audit found silent
    replay failures presenting as merely untested, which is the most
    misleading of the three.
    """
    if status == "quarantined":
        return ["FEATURE_NOT_AVAILABLE_IN_VALIDATION_DATA"]
    if status in ("validated", "high_confidence", "retired", "rejected"):
        return []
    trades = int(cumulative.get("trades") or 0)
    if trades == 0:
        tried = attempts or {}
        errors = int(tried.get("errors") or 0)
        zero = int(tried.get("zeroTrades") or 0)
        if errors:
            return [f"DATA_FAILURE {errors} market(s) errored during replay"]
        if zero:
            return [f"RULE_NEVER_FIRED in {zero} attempted market(s)"]
        return ["NO_OOS_EVENTS_YET"]
    out: list[str] = []
    markets = int(cumulative.get("markets") or 0)
    expectancy = float(cumulative.get("expectancy") or 0.0)
    top_share = float(cumulative.get("top_share") or 0.0)
    ceiling = float(getattr(cfg, "oos_max_concentration", 0.7))
    if trades < cfg.oos_min_trades:
        out.append(f"OOS_TRADES {trades}/{cfg.oos_min_trades}")
    if markets < cfg.oos_min_markets:
        out.append(f"OOS_MARKET_BREADTH {markets}/{cfg.oos_min_markets}")
    if expectancy <= cfg.oos_min_expectancy:
        out.append(f"OOS_EXPECTANCY {expectancy:+.4f} "
                   f"(need > {cfg.oos_min_expectancy:+g})")
    if top_share > ceiling:
        out.append(f"EVENT_CONCENTRATION {top_share:.0%} "
                   f"(max {ceiling:.0%})")
    if status in ("watch", "degraded"):
        out.append("RECENT_DETERIORATION")
    return out


def evidence_score(cumulative: dict, cfg) -> float:
    """The composite evidence score the operator specified — one number in
    0..1 that cannot be dragged up by a single flattering metric.

    Multiplicative on purpose: sample depth x market breadth x
    sample-honest win confidence x P&L diversification. A zero in any
    dimension zeroes the score; a high in-sample win rate contributes
    nothing at all.
    """
    trades = int(cumulative.get("trades") or 0)
    if trades <= 0 or float(cumulative.get("expectancy") or 0.0) <= 0:
        return 0.0
    sample = min(1.0, trades / max(1, cfg.oos_hc_trades))
    breadth = min(1.0, int(cumulative.get("markets") or 0)
                  / max(1, cfg.oos_hc_markets))
    wins = int(cumulative.get("wins") or 0)
    p = wins / trades
    z = 1.96
    denom = 1 + z * z / trades
    centre = p + z * z / (2 * trades)
    margin = z * ((p * (1 - p) + z * z / (4 * trades)) / trades) ** 0.5
    wilson = max(0.0, (centre - margin) / denom)
    diversification = 1.0 - float(cumulative.get("top_share") or 0.0)
    return round(sample * breadth * wilson * max(0.0, diversification), 4)
