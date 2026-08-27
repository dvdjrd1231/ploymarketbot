"""Read-only access to the V1 historical databases.

V1's `intel.sqlite3` holds the only substantial history this project has:
878,650 wallet trades, 467,053 raw events, 8,116 resolutions, ~90 days. V3
reads it and never writes to it — the connection is opened with
`mode=ro` *and* `PRAGMA query_only`, which is belt and braces on purpose: the
URI flag protects against a bug in our SQL, the pragma protects against a bug
in our connection handling.

Everything here is streamed and bounded. Nothing loads the file into RAM.
"""

from __future__ import annotations

import sqlite3
import threading

from functools import lru_cache
from pathlib import Path
from typing import Iterator

from ..config import Settings

DECISION_EVENT = "TRADE"

_POOL = threading.local()


def connect_ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15.0,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA cache_size = -64000")
    return conn


def pooled_ro(db: Path) -> sqlite3.Connection:
    """One long-lived read-only connection per (thread, database).

    PROFILE FIRST, and this is what the profile said. A 300-market scan spent
    8.0s of which 7.5s was inside `sqlite3.execute` and 6.65s of THAT was
    inside connection setup — 776 connections at ~8.5ms each, because
    `PRAGMA cache_size = -64000` asks for a fresh 64 MB page cache every time.
    The queries themselves were never the problem.

    Reusing the connection removes the setup cost and lets the page cache stay
    warm across queries, which is the larger of the two wins. No new language,
    no equivalence risk, no build toolchain — see `docs/PERFORMANCE.md` for the
    before/after and for what genuinely is CPU-bound (the wallet-DNA pass).
    """
    cache = getattr(_POOL, "conns", None)
    if cache is None:
        cache = _POOL.conns = {}
    key = str(db)
    conn = cache.get(key)
    if conn is None:
        conn = cache[key] = connect_ro(db)
    return conn


class HistoricalSource:
    """Bounded, causal reads over the V1 tape.

    Every method that takes an `as_of` filters with `<=` on the trade
    timestamp. There is no method here that can return a row from the future,
    which is why the point-in-time builder is allowed to call it freely.
    """

    def __init__(self, st: Settings) -> None:
        self.st = st
        self.db = st.data_db
        self.available = self.db.exists()
        # Populated by `use_settlement_times`. Empty means "V1's observation
        # time is all we have", which is safe but blunt — see
        # `ingest/settled_ts.py`.
        self._settled_override: dict = {}

    def _conn(self) -> sqlite3.Connection:
        """A pooled read-only connection. Callers must NOT close it."""
        return pooled_ro(self.db)

    # -- inventory ----------------------------------------------------------
    def inventory(self) -> dict:
        if not self.available:
            return {"available": False, "path": str(self.db)}
        c = self._conn()
        q = lambda s, p=(): c.execute(s, p).fetchone()
        lo, hi = q("SELECT MIN(ts), MAX(ts) FROM wallet_trades")
        res_total = q("SELECT COUNT(*) FROM resolutions")[0]
        res_missing = q(
            "SELECT COUNT(*) FROM resolutions WHERE settled_ts IS NULL "
            "OR settled_ts = 0")[0]
        return {
            "available": True, "path": str(self.db),
            "wallet_trades": q("SELECT COUNT(*) FROM wallet_trades")[0],
            "raw_events": q("SELECT COUNT(*) FROM raw_events")[0],
            "wallets": q("SELECT COUNT(DISTINCT wallet) FROM wallet_trades")[0],
            "markets": q("SELECT COUNT(DISTINCT market_id) FROM wallet_trades")[0],
            "tokens": q("SELECT COUNT(DISTINCT token_id) FROM wallet_trades")[0],
            "resolutions": res_total,
            "resolutions_missing_settled_ts": res_missing,
            "settled_ts_coverage": round(
                1.0 - (res_missing / res_total), 4) if res_total else 0.0,
            "first_ts": int(lo or 0), "last_ts": int(hi or 0),
            "tape_days": round((int(hi or 0) - int(lo or 0)) / 86400.0, 1),
        }

    @lru_cache(maxsize=1)
    def latest_ts(self) -> int:
        """The newest timestamp in the tape — the 'data clock'.

        Distinct from the wall clock on purpose. This dataset's last print is
        over a day old, so a scan anchored to `time.time()` looking back six
        hours correctly finds nothing at all. Anchoring research to the data
        clock is what lets the scanner work on stale history; anchoring
        *trading* to it would be catastrophic, which is why
        `DATA_VALIDITY` still measures staleness against the wall clock and
        refuses.

        Filtered to `event_type = 'TRADE'` deliberately. REDEEM, MERGE, SPLIT
        and CONVERSION are mechanical position operations that keep arriving
        for days after the last actual decision — on this dataset the newest
        row is a REDEEM 29 hours after the newest TRADE. Anchoring to the
        newest row of any type would place the scan in a window containing
        nothing anyone chose to do.
        """
        if not self.available:
            return 0
        c = self._conn()
        return int(c.execute(
            "SELECT MAX(ts) FROM wallet_trades WHERE event_type = ?",
            (DECISION_EVENT,)).fetchone()[0] or 0)

    def data_lag_secs(self) -> int:
        import time as _t
        hi = self.latest_ts()
        return max(0, int(_t.time()) - hi) if hi else -1

    # -- markets ------------------------------------------------------------
    def market_meta(self, market_id: str, as_of: int = 0) -> dict:
        """Tape-derived market facts, bounded at `as_of`.

        The bound is not optional. Without it `last_ts` is the market's newest
        print of all time, which for a point-in-time state build is a
        timestamp from the future — V3's INFORMATION_VALIDITY gate catches
        exactly this and refuses to trade, which is how the missing bound was
        found.
        """
        c = self._conn()
        sql = ("SELECT market_id, MAX(question) q, COUNT(*) n, "
               "       MIN(ts) first_ts, MAX(ts) last_ts, "
               "       COUNT(DISTINCT token_id) tokens, "
               "       COUNT(DISTINCT wallet) wallets "
               "  FROM wallet_trades WHERE market_id = ? ")
        params: list = [market_id]
        if as_of:
            sql += " AND ts <= ? "
            params.append(as_of)
        sql += " GROUP BY market_id"
        r = c.execute(sql, params).fetchone()
        return dict(r) if r else {}

    def active_markets(self, as_of: int, lookback_secs: int = 86_400,
                       limit: int = 500) -> list[dict]:
        """Markets that printed inside the lookback window ending at `as_of`."""
        c = self._conn()
        rows = c.execute(
            "SELECT market_id, MAX(question) question, COUNT(*) prints, "
            "       COUNT(DISTINCT wallet) wallets, SUM(usdc) notional, "
            "       MAX(ts) last_ts "
            "  FROM wallet_trades "
            " WHERE event_type = ? AND ts <= ? AND ts > ? "
            "   AND market_id != '' "
            " GROUP BY market_id ORDER BY notional DESC LIMIT ?",
            (DECISION_EVENT, as_of, as_of - lookback_secs, limit)).fetchall()
        return [dict(r) for r in rows]

    def tokens_for_market(self, market_id: str, as_of: int) -> list[dict]:
        c = self._conn()
        rows = c.execute(
            "SELECT token_id, MAX(outcome) outcome, COUNT(*) prints, "
            "       MAX(ts) last_ts "
            "  FROM wallet_trades WHERE market_id=? AND ts<=? AND event_type=? "
            " GROUP BY token_id ORDER BY prints DESC",
            (market_id, as_of, DECISION_EVENT)).fetchall()
        return [dict(r) for r in rows]

    # -- price tape ---------------------------------------------------------
    def prints(self, token_id: str, as_of: int, lookback_secs: int = 86_400,
               limit: int = 4000) -> list[tuple[int, float, float, str]]:
        """(ts, price, usdc, side) for one token, oldest first, up to `as_of`."""
        c = self._conn()
        rows = c.execute(
            "SELECT ts, price, usdc, side FROM wallet_trades "
            " WHERE token_id=? AND event_type=? AND ts<=? AND ts>? "
            "   AND price>0 AND price<1 "
            " ORDER BY ts ASC LIMIT ?",
            (token_id, DECISION_EVENT, as_of, as_of - lookback_secs,
             limit)).fetchall()
        return [(int(r[0]), float(r[1]), float(r[2] or 0.0), r[3] or "")
                for r in rows]

    def active_tokens(self, *, min_prints: int = 40, min_distinct: int = 8,
                      limit: int = 60) -> list[tuple[str, int, int, int]]:
        """(token_id, prints, first_ts, last_ts) for tokens worth analysing.

        `min_distinct` filters out tokens that traded thousands of times at a
        single price — this database has several, and they carry no variance
        for any estimator to work with. Ordered by activity so a caller taking
        the first N gets the most informative N.
        """
        c = self._conn()
        rows = c.execute(
            "SELECT token_id, COUNT(*) n, MIN(ts) t0, MAX(ts) t1 "
            "  FROM wallet_trades "
            " WHERE event_type=? AND token_id != '' AND price>0 AND price<1 "
            " GROUP BY token_id "
            "HAVING n >= ? AND COUNT(DISTINCT price) >= ? "
            " ORDER BY n DESC LIMIT ?",
            (DECISION_EVENT, min_prints, min_distinct, limit)).fetchall()
        return [(r[0], int(r[1]), int(r[2]), int(r[3])) for r in rows]

    def last_price(self, token_id: str, as_of: int) -> tuple[int, float] | None:
        c = self._conn()
        r = c.execute(
            "SELECT ts, price FROM wallet_trades WHERE token_id=? "
            "AND event_type=? AND ts<=? AND price>0 AND price<1 "
            "ORDER BY ts DESC LIMIT 1",
            (token_id, DECISION_EVENT, as_of)).fetchone()
        return (int(r[0]), float(r[1])) if r else None

    # -- wallets ------------------------------------------------------------
    def wallet_trades(self, wallet: str, as_of: int = 0,
                      limit: int = 5000) -> list[dict]:
        c = self._conn()
        sql = ("SELECT ts, token_id, market_id, outcome, side, price, size, "
               "       usdc, question, event_type "
               "  FROM wallet_trades WHERE wallet=? ")
        params: list = [wallet]
        if as_of:
            sql += " AND ts<=? "
            params.append(as_of)
        sql += " ORDER BY ts ASC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in c.execute(sql, params).fetchall()]

    def wallets_in_market(self, market_id: str, as_of: int,
                          lookback_secs: int = 86_400) -> list[dict]:
        c = self._conn()
        rows = c.execute(
            "SELECT wallet, COUNT(*) n, SUM(usdc) notional, "
            "       AVG(price) avg_price, MAX(ts) last_ts "
            "  FROM wallet_trades "
            " WHERE market_id=? AND event_type=? AND side='BUY' "
            "   AND ts<=? AND ts>? "
            " GROUP BY wallet ORDER BY notional DESC LIMIT 200",
            (market_id, DECISION_EVENT, as_of,
             as_of - lookback_secs)).fetchall()
        return [dict(r) for r in rows]

    @lru_cache(maxsize=1)
    def _candidate_wallets_cached(self) -> tuple:
        c = self._conn()
        rows = c.execute(
            "SELECT t.wallet, COUNT(*) n FROM wallet_trades t "
            "  JOIN resolutions r ON t.token_id = r.token_id "
            " WHERE t.event_type=? AND t.side='BUY' AND r.price IN (0.0,1.0) "
            " GROUP BY t.wallet HAVING n >= 60 ORDER BY n DESC LIMIT 4000",
            (DECISION_EVENT,)).fetchall()
        return tuple((r[0], int(r[1])) for r in rows)

    def candidate_wallets(self) -> list[tuple[str, int]]:
        """Wallets with enough SETTLED evidence to be evaluable.

        A count, never a quality judgement. Ranking wallets by realised profit
        here would be the wallet-selection leakage the brief forbids — the
        ranking has to be point-in-time and lives in `intelligence/wallets.py`.
        """
        if not self.available:
            return []
        return list(self._candidate_wallets_cached())

    # -- settled trades -----------------------------------------------------
    def use_settlement_times(self, store) -> int:
        """Prefer V3's measured settlement timestamps over V1's fallback.

        Loads only rows whose method is trustworthy enough to change a
        decision (confidence >= 0.60). Loading the V1_FALLBACK tier would
        overwrite V1's own value with a copy of itself and inflate the apparent
        coverage — see `ingest/settled_ts.py` for why the tiers are not blended.
        """
        rows = store.query(
            "SELECT token_id, settled_ts FROM resolution_times "
            " WHERE confidence >= 0.60 AND settled_ts > 0")
        self._settled_override = {r["token_id"]: int(r["settled_ts"])
                                  for r in rows}
        return len(self._settled_override)

    def iter_settled(self, *, as_of: int = 0, wallets: tuple = (),
                     chunk: int = 50_000) -> Iterator[dict]:
        c = self._conn()
        sql = ("SELECT t.wallet, t.ts, t.token_id, t.market_id, t.outcome, "
               "       t.price, t.size, t.usdc, r.price resolution, "
               "       CASE WHEN r.settled_ts > 0 THEN r.settled_ts "
               "            ELSE r.ts END settled_ts, t.question "
               "  FROM wallet_trades t "
               "  JOIN resolutions r ON t.token_id = r.token_id "
               " WHERE t.event_type=? AND t.side='BUY' "
               "   AND t.price>? AND t.price<? AND r.price IN (0.0,1.0) ")
        params: list = [DECISION_EVENT, self.st.costs.min_price,
                        self.st.costs.max_price]
        if wallets:
            sql += f" AND t.wallet IN ({','.join('?' * len(wallets))})"
            params.extend(wallets)
        if as_of:
            sql += " AND t.ts <= ?"
            params.append(as_of)
        sql += " ORDER BY t.ts ASC"
        cur = c.execute(sql, params)
        while True:
            rows = cur.fetchmany(chunk)
            if not rows:
                return
            for r in rows:
                d = dict(r)
                better = self._settled_override.get(d["token_id"])
                if better:
                    d["settled_ts"] = better
                    d["settled_ts_source"] = "V3_MEASURED"
                else:
                    d["settled_ts_source"] = "V1_OBSERVED"
                yield d

    def resolution_for(self, token_id: str) -> dict | None:
        """The OUTCOME. Only ever called by scoring code, never by a gate.

        Deliberately not exposed on `EvidenceState`: an agent that could reach
        this would be reading the answer sheet.
        """
        c = self._conn()
        r = c.execute(
            "SELECT token_id, price, ts, settled_ts FROM resolutions "
            " WHERE token_id=?", (token_id,)).fetchone()
        return dict(r) if r else None

    def price_band_baseline(self, lo: float, hi: float) -> dict:
        """Market-wide outcome rate inside a price band.

        The favourite–longshot control. On this dataset a plain "buy between
        0.60 and 0.80" earns roughly +9 points of expectancy while copying
        nobody, so any wallet strategy that lives in that band must be scored
        against this baseline before it may be called wallet alpha.
        """
        c = self._conn()
        r = c.execute(
            "SELECT COUNT(*) n, AVG(r.price) hit_rate, "
            "       AVG((r.price - t.price)/t.price) mean_ret "
            "  FROM wallet_trades t JOIN resolutions r "
            "    ON t.token_id = r.token_id "
            " WHERE t.event_type=? AND t.side='BUY' "
            "   AND r.price IN (0.0,1.0) AND t.price>=? AND t.price<?",
            (DECISION_EVENT, lo, hi)).fetchone()
        return {"n": int(r[0] or 0), "hit_rate": float(r[1] or 0.0),
                "mean_return": float(r[2] or 0.0), "band": [lo, hi]}
