"""The settled-event substrate, and the tape-derived price series.

This module is the reason the engine exists in this shape. See docs/AUDIT.md:
the previous pipeline validated strategies against 123 captured feature series
(~78k rows, 3.8 days). The same database holds 189,446 wallet trades that join
directly to a *settled resolution* across 4,244 tokens and 112 days. Scoring a
trade against its resolution needs no order book and no captured series, so the
evaluable universe is ~34x larger on markets and ~30x longer in time.

Two objects come out of here:

  SettledTrade  one wallet BUY whose token later resolved to 0.0 or 1.0.
                Hold-to-resolution P&L is then exact, not modelled.

  PriceTape     every trade in the database, by token, in time order. Used to
                answer "what price could I actually have paid `delay` seconds
                after the wallet acted" (§30) without an order book. This is a
                real, causal, executable price — someone traded there.

Both are streamed and chunked; nothing here loads the 2.4 GB database into RAM
(§22).
"""

from __future__ import annotations

import bisect
import sqlite3
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from .config import Settings

# Only these are decisions. REDEEM / MERGE / SPLIT / CONVERSION / REWARD are
# mechanical position operations — copying them is meaningless and including
# them would inflate every trade count in the system.
DECISION_EVENT = "TRADE"


@dataclass(frozen=True, slots=True)
class SettledTrade:
    """One copyable wallet action with a known outcome.

    `resolution` is the settlement value of *this* token (1.0 if the outcome
    happened, 0.0 if not), so the payoff of buying one share at `price` and
    holding is exactly `resolution - price`.
    """

    wallet: str
    ts: int
    token_id: str
    market_id: str
    outcome: str
    price: float
    size: float
    usdc: float
    resolution: float
    settled_ts: int
    question: str

    @property
    def won(self) -> bool:
        return self.resolution > 0.5

    def gross_return(self) -> float:
        """Return on capital if held to resolution, before costs."""
        return (self.resolution - self.price) / self.price


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    # Big sequential scans over a 2.4 GB file; a modest page cache beats the
    # default without risking the 16 GB budget.
    conn.execute("PRAGMA cache_size = -64000")  # 64 MB
    return conn


# NOTE on the settlement clock, which is a real limitation of this dataset.
#
# `resolutions.settled_ts` is 0.0 in all 8,116 rows -- the ingester never
# populated it. So the true moment an outcome became public is NOT recorded
# anywhere in this database.
#
# `resolutions.ts` (when the system OBSERVED the resolution) is populated, and
# is later than the trade in 100% of joined rows. It is therefore a safe upper
# bound: using it can only DELAY the moment an outcome enters wallet state,
# never advance it, so it cannot leak (S20). It is weak rather than wrong --
# its range spans just 7.4 days, so trades older than that all appear to settle
# at once, which suppresses wallet track-record features over most of the tape.
#
# Consequence, stated so nobody reads more into the results than they support:
# point-in-time wallet track record cannot be faithfully reconstructed from
# this data. Capturing true settlement time is the fix, and it is listed in
# docs/AUDIT.md as a required backfill.
_SETTLED_COLS = """
       t.wallet, t.ts, t.token_id, t.market_id, t.outcome,
       t.price, t.size, t.usdc, r.price,
       CASE WHEN r.settled_ts > 0 THEN r.settled_ts ELSE r.ts END,
       t.question
"""

# The FROM/WHERE half, shared by every query so the "evaluable universe" has
# exactly one definition. Callers supply their own SELECT list.
_SETTLED_FROM = """
  FROM wallet_trades t
  JOIN resolutions  r ON t.token_id = r.token_id
 WHERE t.event_type = ?
   AND t.side = 'BUY'
   AND t.price > ? AND t.price < ?
   AND t.usdc >= ?
   AND r.price IN (0.0, 1.0)
"""

_SETTLED_SQL = "SELECT" + _SETTLED_COLS + _SETTLED_FROM


def iter_settled(
    st: Settings,
    *,
    wallets: Sequence[str] | None = None,
    min_notional: float = 1.0,
    order_by_time: bool = True,
) -> Iterator[SettledTrade]:
    """Stream settled BUY trades, oldest first.

    Ordering by time matters: every consumer in this engine is causal, and a
    chronological stream lets wallet state be built forward without ever
    holding the whole history.
    """
    sql = _SETTLED_SQL
    params: list = [DECISION_EVENT, st.costs.min_price, st.costs.max_price, min_notional]
    if wallets:
        marks = ",".join("?" * len(wallets))
        sql += f" AND t.wallet IN ({marks})"
        params.extend(wallets)
    if order_by_time:
        sql += " ORDER BY t.ts ASC"

    conn = _connect(st.data_db)
    try:
        cur = conn.execute(sql, params)
        while True:
            rows = cur.fetchmany(st.chunk_rows)
            if not rows:
                return
            for r in rows:
                # settled_ts is occasionally null in the tape; fall back to the
                # resolution timestamp being unknown-but-after the trade. We
                # never use a null as "known now" — see wallets.py.
                yield SettledTrade(
                    wallet=r[0], ts=int(r[1]), token_id=r[2], market_id=r[3] or "",
                    outcome=r[4] or "", price=float(r[5]), size=float(r[6]),
                    usdc=float(r[7]), resolution=float(r[8]),
                    settled_ts=int(r[9]) if r[9] is not None else 0,
                    question=r[10] or "",
                )
    finally:
        conn.close()


def wallet_trade_counts(st: Settings, min_trades: int = 50) -> list[tuple[str, int]]:
    """Wallets ranked by how much evaluable evidence they carry.

    This is a *count*, not a quality judgement — quality ranking is point-in-
    time and lives in wallets.py (§43).
    """
    conn = _connect(st.data_db)
    try:
        sql = "SELECT t.wallet, COUNT(*) n" + _SETTLED_FROM
        sql += " GROUP BY t.wallet HAVING n >= ? ORDER BY n DESC"
        rows = conn.execute(
            sql,
            [DECISION_EVENT, st.costs.min_price, st.costs.max_price, 1.0, min_trades],
        ).fetchall()
        return [(w, int(n)) for w, n in rows]
    finally:
        conn.close()


class PriceTape:
    """Per-token trade prints, in time order, for delayed-execution pricing.

    The aggregate tape of all ~70k wallets is itself a price series: if anyone
    traded a token at time T, that is a price you could plausibly have paid at
    time T. It is coarser than an order book and it is honest about coverage —
    `price_at` returns None rather than interpolating a price nobody printed.

    Loaded lazily per token and bounded by an LRU cap so a sweep over thousands
    of tokens cannot grow without limit (§22).
    """

    def __init__(self, st: Settings, max_tokens: int = 4_000) -> None:
        self._st = st
        self._max = max_tokens
        self._cache: dict[str, tuple[list[int], list[float]]] = {}
        self._order: list[str] = []

    def _load(self, token_id: str) -> tuple[list[int], list[float]]:
        hit = self._cache.get(token_id)
        if hit is not None:
            return hit
        conn = _connect(self._st.data_db)
        try:
            rows = conn.execute(
                "SELECT ts, price FROM wallet_trades "
                " WHERE token_id = ? AND event_type = ? AND price > 0 AND price < 1 "
                " ORDER BY ts ASC",
                (token_id, DECISION_EVENT),
            ).fetchall()
        finally:
            conn.close()
        ts = [int(a) for a, _ in rows]
        px = [float(b) for _, b in rows]
        self._cache[token_id] = (ts, px)
        self._order.append(token_id)
        if len(self._order) > self._max:
            self._cache.pop(self._order.pop(0), None)
        return ts, px

    def price_at(self, token_id: str, at_ts: int, window: int = 3600) -> float | None:
        """First printed price at or after `at_ts`, within `window` seconds.

        Returns None when nothing printed inside the window — the honest answer
        when the copy could not have been executed at a known price.
        """
        ts, px = self._load(token_id)
        if not ts:
            return None
        i = bisect_left(ts, at_ts)
        if i >= len(ts):
            return None
        if ts[i] - at_ts > window:
            return None
        return px[i]

    def coverage(self, token_id: str) -> int:
        return len(self._load(token_id)[0])


def inventory(st: Settings) -> dict:
    """Measure the substrate. This is the §26/§49 benchmark, as a function.

    Deliberately cheap so it can be re-run as backfill grows and the numbers in
    docs/AUDIT.md can be checked rather than believed.
    """
    conn = _connect(st.data_db)
    try:
        q = lambda s, p=(): conn.execute(s, p).fetchone()[0]
        out = {
            "engine": "walletlab",
            "wallet_trades_total": q("SELECT COUNT(*) FROM wallet_trades"),
            "wallets_total": q("SELECT COUNT(DISTINCT wallet) FROM wallet_trades"),
            "markets_total": q("SELECT COUNT(DISTINCT market_id) FROM wallet_trades"),
            "resolutions": q("SELECT COUNT(*) FROM resolutions"),
        }
        lo, hi = conn.execute("SELECT MIN(ts), MAX(ts) FROM wallet_trades").fetchone()
        out["tape_first_ts"] = int(lo or 0)
        out["tape_last_ts"] = int(hi or 0)
        out["tape_days"] = round((int(hi or 0) - int(lo or 0)) / 86400.0, 1)

        params = [DECISION_EVENT, st.costs.min_price, st.costs.max_price, 1.0]
        cnt_sql = ("SELECT COUNT(*), COUNT(DISTINCT t.wallet), "
                   "COUNT(DISTINCT t.token_id)" + _SETTLED_FROM)
        n, w, tk = conn.execute(cnt_sql, params).fetchone()
        out["settled_copyable_trades"] = int(n)
        out["settled_wallets"] = int(w)
        out["settled_tokens"] = int(tk)

        for k in (50, 100, 200, 500):
            g = ("SELECT t.wallet, COUNT(*) c" + _SETTLED_FROM
                 + " GROUP BY t.wallet HAVING c >= ?")
            out[f"wallets_ge_{k}_settled"] = int(
                conn.execute(f"SELECT COUNT(*) FROM ({g})", params + [k]).fetchone()[0]
            )
        return out
    finally:
        conn.close()
