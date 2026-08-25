"""The evaluable substrate: settled wallet trades and a tape-derived price path.

Measured on the client's own database on 2026-08-24, and this measurement is
the reason V2 exists in this shape:

    what pqb/research.py validates against      78,219 rows,   123 markets,   3.8 days
    wallet_trades JOIN settled resolutions     136,581 rows, 1,609 markets,  90.0 days

The V1 engine is not badly written. It is starved. It is asked to prove a
strategy out-of-sample using ~1.6% of the evidence sitting in the same file,
which is why 234 discovered strategies produced 0 validated ones, which is why
learning mode never opened, which is why all 40,820 decisions were DO_NOTHING.
One cause, four symptoms.

Scoring a hold-to-resolution trade needs no order book: buy at `p`, hold, the
payoff is exactly `resolution - p` with `resolution` in {0, 1}. That is why
this substrate is available and the captured-feature substrate is not.

Everything is streamed. Nothing loads the 2.6 GB database into RAM.
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from ..config import Settings

# REDEEM / MERGE / SPLIT / CONVERSION / REWARD are mechanical position
# operations, not decisions. Copying them is meaningless and counting them
# would inflate every trade-frequency number in the system.
DECISION_EVENT = "TRADE"


@dataclass(frozen=True, slots=True)
class SettledTrade:
    """One copyable wallet BUY whose token later resolved to 0.0 or 1.0."""

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
        return (self.resolution - self.price) / self.price if self.price > 0 else 0.0


def connect(db: Path) -> sqlite3.Connection:
    """Read-only. V2 never writes to the V1 data file (non-negotiable rule 1)."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA cache_size = -64000")      # 64 MB, inside a 16 GB budget
    return conn


# `resolutions.settled_ts` is 0.0 in all 8,116 rows -- the V1 ingester never
# populated it, and this is gap #1 in the client's own GAPS.md. So the true
# moment an outcome became public is not recorded anywhere.
#
# `resolutions.ts` (when the system OBSERVED the resolution) is populated and is
# later than the trade in 100% of joined rows. Using it can only DELAY the
# moment an outcome enters wallet state, never advance it -- so it cannot leak
# (rule 7). It is weak rather than wrong: its range spans ~7.4 days, so older
# trades all appear to settle at once, which suppresses wallet track-record
# features over most of the tape.
#
# Consequence, stated plainly: point-in-time wallet track record is
# reconstructible but blunt on this data. Capturing true settlement time is the
# single highest-value backfill in the project. Recorded in docs/LIMITS.md.
_COLS = """
       t.wallet, t.ts, t.token_id, t.market_id, t.outcome,
       t.price, t.size, t.usdc, r.price,
       CASE WHEN r.settled_ts > 0 THEN r.settled_ts ELSE r.ts END,
       t.question
"""

_FROM = """
  FROM wallet_trades t
  JOIN resolutions  r ON t.token_id = r.token_id
 WHERE t.event_type = ?
   AND t.side = 'BUY'
   AND t.price > ? AND t.price < ?
   AND t.usdc >= ?
   AND r.price IN (0.0, 1.0)
"""


def iter_settled(st: Settings, *, wallets: Sequence[str] | None = None,
                 min_notional: float = 1.0,
                 ts_from: int = 0, ts_to: int = 0) -> Iterator[SettledTrade]:
    """Stream settled BUYs oldest-first.

    Chronological order is not cosmetic: every consumer downstream is causal,
    and a forward stream lets wallet state be built without ever holding the
    whole history.
    """
    sql = "SELECT" + _COLS + _FROM
    params: list = [DECISION_EVENT, st.costs.min_price, st.costs.max_price,
                    min_notional]
    if wallets:
        sql += f" AND t.wallet IN ({','.join('?' * len(wallets))})"
        params.extend(wallets)
    if ts_from:
        sql += " AND t.ts >= ?"
        params.append(ts_from)
    if ts_to:
        sql += " AND t.ts < ?"
        params.append(ts_to)
    sql += " ORDER BY t.ts ASC"

    conn = connect(st.data_db)
    try:
        cur = conn.execute(sql, params)
        while True:
            rows = cur.fetchmany(st.chunk_rows)
            if not rows:
                return
            for r in rows:
                yield SettledTrade(
                    wallet=r[0], ts=int(r[1]), token_id=r[2],
                    market_id=r[3] or "", outcome=r[4] or "",
                    price=float(r[5]), size=float(r[6]), usdc=float(r[7]),
                    resolution=float(r[8]),
                    settled_ts=int(r[9]) if r[9] is not None else 0,
                    question=r[10] or "")
    finally:
        conn.close()


def wallet_trade_counts(st: Settings, min_trades: int = 50) -> list[tuple[str, int]]:
    """Wallets ranked by how much evaluable evidence they carry.

    A count, never a quality judgement -- quality ranking must be point-in-time
    and lives in `state.py`. Ranking wallets by realised profit here would be
    the wallet-selection leakage the brief forbids.
    """
    conn = connect(st.data_db)
    try:
        sql = ("SELECT t.wallet, COUNT(*) n" + _FROM
               + " GROUP BY t.wallet HAVING n >= ? ORDER BY n DESC")
        rows = conn.execute(sql, [DECISION_EVENT, st.costs.min_price,
                                  st.costs.max_price, 1.0, min_trades]).fetchall()
        return [(w, int(n)) for w, n in rows]
    finally:
        conn.close()


def time_bounds(st: Settings) -> tuple[int, int]:
    conn = connect(st.data_db)
    try:
        sql = "SELECT MIN(t.ts), MAX(t.ts)" + _FROM
        lo, hi = conn.execute(sql, [DECISION_EVENT, st.costs.min_price,
                                    st.costs.max_price, 1.0]).fetchone()
        return int(lo or 0), int(hi or 0)
    finally:
        conn.close()


def oos_split_ts(st: Settings) -> int:
    """The timestamp separating in-sample from out-of-sample.

    Split by TIME, never by row: a random split over a tape where the same
    market appears many times leaks the market's outcome into training.
    """
    lo, hi = time_bounds(st)
    return int(lo + (hi - lo) * (1.0 - st.oos_fraction))


class PriceTape:
    """Per-token prints in time order, for delayed-execution pricing and a
    coarse price path.

    The aggregate tape of ~70k wallets is itself a price series: if anyone
    printed a token at time T, that is a price you could plausibly have paid at
    T. Coarser than an order book, and honest about coverage -- `price_at`
    returns None rather than interpolating a price nobody printed. That one
    line is the difference between a real copy backtest and a fictional one.

    LRU-bounded so a sweep over thousands of tokens cannot grow without limit.
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
        conn = connect(self._st.data_db)
        try:
            rows = conn.execute(
                "SELECT ts, price FROM wallet_trades "
                " WHERE token_id = ? AND event_type = ? AND price > 0 AND price < 1 "
                " ORDER BY ts ASC", (token_id, DECISION_EVENT)).fetchall()
        finally:
            conn.close()
        pair = ([int(a) for a, _ in rows], [float(b) for _, b in rows])
        self._cache[token_id] = pair
        self._order.append(token_id)
        if len(self._order) > self._max:
            self._cache.pop(self._order.pop(0), None)
        return pair

    def price_at(self, token_id: str, at_ts: int,
                 window: int = 3600) -> float | None:
        """First printed price at or after `at_ts`, within `window` seconds.

        None when nothing printed inside the window -- the honest answer when
        the copy could not have been executed at a knowable price.
        """
        ts, px = self._load(token_id)
        if not ts:
            return None
        i = bisect_left(ts, at_ts)
        if i >= len(ts) or ts[i] - at_ts > window:
            return None
        return px[i]

    def path(self, token_id: str, start_ts: int,
             end_ts: int) -> list[tuple[int, float]]:
        """Every print in [start_ts, end_ts). The substrate for early-exit
        research -- see research/exits.py, which states what it cannot answer.
        """
        ts, px = self._load(token_id)
        lo = bisect_left(ts, start_ts)
        hi = bisect_right(ts, end_ts)
        return list(zip(ts[lo:hi], px[lo:hi]))

    def coverage(self, token_id: str) -> int:
        return len(self._load(token_id)[0])


def inventory(st: Settings) -> dict:
    """Measure the substrate. Cheap on purpose, so the numbers in the docs can
    be re-checked as backfill lands rather than believed.
    """
    conn = connect(st.data_db)
    try:
        q = lambda s, p=(): conn.execute(s, p).fetchone()
        params = [DECISION_EVENT, st.costs.min_price, st.costs.max_price, 1.0]
        out: dict = {
            "wallet_trades_total": q("SELECT COUNT(*) FROM wallet_trades")[0],
            "wallets_total": q("SELECT COUNT(DISTINCT wallet) FROM wallet_trades")[0],
            "markets_total": q("SELECT COUNT(DISTINCT market_id) FROM wallet_trades")[0],
            "resolutions": q("SELECT COUNT(*) FROM resolutions")[0],
            "resolutions_missing_settled_ts": q(
                "SELECT COUNT(*) FROM resolutions WHERE settled_ts = 0")[0],
        }
        lo, hi = q("SELECT MIN(ts), MAX(ts) FROM wallet_trades")
        out["tape_days"] = round((int(hi or 0) - int(lo or 0)) / 86400.0, 1)

        n, w, tk, mk, t0, t1 = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT t.wallet), COUNT(DISTINCT t.token_id),"
            " COUNT(DISTINCT t.market_id), MIN(t.ts), MAX(t.ts)" + _FROM,
            params).fetchone()
        out.update(settled_copyable_trades=int(n), settled_wallets=int(w),
                   settled_tokens=int(tk), settled_markets=int(mk),
                   settled_days=round((int(t1 or 0) - int(t0 or 0)) / 86400.0, 1))
        for k in (50, 100, 200, 500):
            g = ("SELECT t.wallet, COUNT(*) c" + _FROM
                 + " GROUP BY t.wallet HAVING c >= ?")
            out[f"wallets_ge_{k}_settled"] = int(
                conn.execute(f"SELECT COUNT(*) FROM ({g})", params + [k]).fetchone()[0])
        out["oos_split_ts"] = int(t0 + (t1 - t0) * (1.0 - st.oos_fraction)) if t1 else 0
        return out
    finally:
        conn.close()
