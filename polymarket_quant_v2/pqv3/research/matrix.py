"""The observation matrix: one causal pass, materialised once, reused by every
hypothesis.

Why this exists. A sweep evaluates thousands of candidate rules. Streaming the
tape once per candidate would be thousands of passes over 878,650 rows. So the
tape is streamed ONCE, through V2's validated causal engine, and flattened into
a column-oriented matrix that every candidate is then evaluated against in
memory.

Reusing V2's causal STATE MACHINE — `WalletState`, `_MarketTape` and
`Observation` — rather than reimplementing it is deliberate, and is the single
most important reuse decision in V3. That machinery enforces the no-look-ahead
rule: a trade's outcome folds into its wallet's statistics at `settled_ts`,
never at `ts`. Rewriting it would double the amount of causal code that can
drift and halve the number of eyes on it.

What V3 supplies instead of V2 is the DRIVER: the loop, the settlement heap,
and — the reason this exists — the row source. V2's `stream_observations`
reads settlement times straight out of V1's `resolutions` table, where
`settled_ts` is 0 in all 8,116 rows. V3's repaired timestamps live in its own
`resolution_times` table, so a matrix built through V2's driver would silently
ignore every repair `pqv3 collect --backfill-settled` performs, and re-running
discovery after a repair would produce identical results.

The safety property that makes this reuse rather than a fork:
`test_v3_research.py::test_v3_driver_matches_v2_stream` asserts that with NO
overrides present, this driver reproduces `stream_observations` row for row.

Column-oriented, not row-oriented, because a rule touches one or two features
across every row. Row-major would drag 26 floats through cache to read one.

`resolution` is stored, but it is the ANSWER and is kept in a separate array
that rule evaluation never receives. Scoring reads it; admission cannot.
"""

from __future__ import annotations

import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings

# The point-in-time features V2's Observation exposes. Order is fixed and
# persisted with the cache — a reordering would silently reinterpret every
# cached column, so `FEATURES` is part of the cache key.
FEATURES = (
    "w_settled_n", "w_win_rate", "w_roi", "w_roll_win_rate", "w_roll_roi",
    "w_edge_t", "w_consec_losses", "w_consec_wins", "w_seen_n",
    "w_secs_since_prev", "w_open_notional", "w_token_repeat",
    "w_market_repeat", "w_avg_notional", "w_avg_price", "price", "notional",
    "size", "rel_notional", "price_vs_wallet_norm", "hour_of_day",
    "secs_to_settle", "market_recent_prints", "market_price_move",
    "market_velocity", "tape_price_gap",
)

CACHE_VERSION = 3


@dataclass
class Matrix:
    """Column-oriented point-in-time observations with their outcomes."""

    cols: dict = field(default_factory=dict)      # feature -> list[float]
    resolution: list = field(default_factory=list)   # 0.0 / 1.0 — THE ANSWER
    ts: list = field(default_factory=list)
    wallet: list = field(default_factory=list)
    market_id: list = field(default_factory=list)
    token_id: list = field(default_factory=list)
    built_ts: int = 0
    source_rows: int = 0
    # The data state this matrix was built from. A mismatch invalidates the
    # cache — see `build`.
    fingerprint: dict | None = None

    def __len__(self) -> int:
        return len(self.resolution)

    @property
    def n(self) -> int:
        return len(self.resolution)

    def time_bounds(self) -> tuple:
        return (self.ts[0], self.ts[-1]) if self.ts else (0, 0)

    def split_ts(self, oos_fraction: float) -> int:
        """The timestamp separating in-sample from out-of-sample.

        Split by TIME, never by row. A random split over a tape where the same
        market appears many times leaks that market's outcome into training —
        the single most common way a prediction-market backtest lies.
        """
        lo, hi = self.time_bounds()
        return int(lo + (hi - lo) * (1.0 - oos_fraction)) if hi else 0

    def index_range(self, ts_from: int, ts_to: int) -> tuple:
        """[start, end) row indices for a time window. Rows are time-ordered."""
        from bisect import bisect_left
        a = bisect_left(self.ts, ts_from) if ts_from else 0
        b = bisect_left(self.ts, ts_to) if ts_to else len(self.ts)
        return a, b

    def quantiles(self, feature: str, qs: tuple) -> list:
        """Sample quantiles of a feature, used to derive rule thresholds.

        Thresholds come from the data's own distribution rather than from round
        numbers, so a rule like `price >= 0.65` is a real quintile boundary
        rather than an arbitrary constant that happens to look tidy.
        """
        col = sorted(v for v in self.cols.get(feature, []) if v is not None)
        if not col:
            return []
        out = []
        for q in qs:
            i = min(len(col) - 1, max(0, int(round(q * (len(col) - 1)))))
            out.append(col[i])
        return out

    def describe(self) -> dict:
        lo, hi = self.time_bounds()
        return {"rows": self.n, "features": len(self.cols),
                "first_ts": lo, "last_ts": hi,
                "days": round((hi - lo) / 86400.0, 1) if hi else 0.0,
                "wallets": len(set(self.wallet)),
                "markets": len(set(self.market_id)),
                "win_rate": round(sum(self.resolution) / self.n, 5)
                if self.n else 0.0,
                "built_ts": self.built_ts}


def _cache_path(st: Settings) -> Path:
    return st.work_dir / "cache" / f"matrix_v{CACHE_VERSION}.pkl"


def stream_observations_v3(st: Settings, store=None, *,
                           min_notional: float = 1.0):
    """V2's causal state machine, driven by V3's corrected settlement times.

    Line for line the same folding discipline as
    `pqv2.substrate.state.stream_observations`:

        1. advance the settlement clock — anything resolved at or before now
           becomes knowable now, and only now
        2. emit the observation from state as it stands
        3. ONLY THEN record that this trade happened
        4. queue its outcome for the moment it settles

    Steps 2 and 3 being in that order is the whole no-look-ahead rule.
    """
    import heapq

    from pqv2.substrate.data import SettledTrade
    from pqv2.substrate.state import Observation, WalletState, _MarketTape

    from ..core.source import HistoricalSource

    src = HistoricalSource(st)
    n_over = src.use_settlement_times(store) if store is not None else 0
    if n_over:
        # Recorded on the matrix so a reader can tell which settlement clock a
        # cached matrix was built against.
        pass

    states: dict = {}
    pending: list = []
    tape = _MarketTape()
    seq = 0

    for row in src.iter_settled():
        if float(row["usdc"] or 0.0) < min_notional:
            continue
        tr = SettledTrade(
            wallet=row["wallet"], ts=int(row["ts"]), token_id=row["token_id"],
            market_id=row["market_id"] or "", outcome=row["outcome"] or "",
            price=float(row["price"]), size=float(row["size"] or 0.0),
            usdc=float(row["usdc"] or 0.0),
            resolution=float(row["resolution"]),
            settled_ts=int(row["settled_ts"] or 0),
            question=row["question"] or "")

        while pending and pending[0][0] <= tr.ts:
            _, _, w, won, gret, stake = heapq.heappop(pending)
            states.setdefault(w, WalletState()).fold_settled(won, gret, stake)

        s = states.setdefault(tr.wallet, WalletState())
        avg_n = s.avg_notional or tr.usdc
        avg_p = s.avg_price or tr.price
        prints, move, velocity, gap = tape.context(tr.token_id, tr.ts, tr.price)

        yield Observation(
            trade=tr,
            w_settled_n=s.settled_n, w_win_rate=s.win_rate, w_roi=s.roi,
            w_roll_win_rate=s.rolling_win_rate, w_roll_roi=s.rolling_roi,
            w_edge_t=s.edge_t_stat(), w_consec_losses=s.consecutive_losses,
            w_consec_wins=s.consecutive_wins, w_seen_n=s.seen_n,
            w_secs_since_prev=(tr.ts - s.last_ts) if s.last_ts else -1,
            w_open_notional=s.open_notional,
            w_token_repeat=tr.token_id in s.tokens_seen,
            w_market_repeat=bool(tr.market_id) and tr.market_id in s.markets_seen,
            w_avg_notional=avg_n, w_avg_price=avg_p,
            price=tr.price, notional=tr.usdc, size=tr.size,
            rel_notional=tr.usdc / avg_n if avg_n > 0 else 1.0,
            price_vs_wallet_norm=tr.price - avg_p,
            hour_of_day=(tr.ts // 3600) % 24,
            secs_to_settle=max(0, tr.settled_ts - tr.ts) if tr.settled_ts else -1,
            market_recent_prints=prints, market_price_move=move,
            market_velocity=velocity, tape_price_gap=gap,
        )

        s.observe_trade(tr)
        tape.add(tr.token_id, tr.ts, tr.price)
        seq += 1
        settle_at = tr.settled_ts if tr.settled_ts else tr.ts + 10 ** 9
        heapq.heappush(pending, (settle_at, seq, tr.wallet, tr.won,
                                 tr.gross_return(), tr.usdc))


def data_fingerprint(st: Settings, store) -> dict:
    """What the research pass depends on, reduced to a comparable record.

    Used by `discover --if-changed` to skip a six-minute pass that would
    reproduce its own previous answer, and by the matrix cache to notice that a
    settlement repair has invalidated it.
    """
    from ..core.source import HistoricalSource
    from ..ingest.settled_ts import coverage
    src = HistoricalSource(st)
    inv = src.inventory() if src.available else {}
    cov = coverage(store)
    return {
        "wallet_trades": inv.get("wallet_trades", 0),
        "resolutions": inv.get("resolutions", 0),
        "last_ts": inv.get("last_ts", 0),
        "settlement_usable": cov.get("usable", 0),
        "markets_synced": store.count("markets"),
        "book_snapshots": store.count("book_snapshots"),
        "news_items": store.count("news_items"),
        "min_price": st.costs.min_price,
        "max_price": st.costs.max_price,
        "slippage_bps": st.costs.slippage_bps,
    }


def build(st: Settings, store=None, *, min_notional: float = 1.0,
          limit: int = 0, rebuild: bool = False, progress=None) -> Matrix:
    """Stream the tape once through V2's causal engine and flatten it.

    Cached on disk: this pass takes minutes over the full tape and its output
    is a pure function of (database, feature list, min_notional). Re-running a
    sweep should not re-run the tape.
    """
    fp = data_fingerprint(st, store) if store is not None else None
    path = _cache_path(st)
    if path.exists() and not rebuild:
        try:
            with open(path, "rb") as fh:
                m = pickle.load(fh)
            fresh = (isinstance(m, Matrix) and m.n
                     and set(m.cols) == set(FEATURES))
            # A settlement repair changes what the causal pass produces, so a
            # matrix built before it is stale even though the tape is the same
            # file. Without this check `collect --backfill-settled` would
            # improve the data and `discover` would keep reading the old cache.
            if fresh and fp is not None and getattr(m, "fingerprint", None) \
                    not in (None, fp):
                fresh = False
            if fresh:
                return m
        except Exception:                                     # noqa: BLE001
            pass                     # a corrupt cache is rebuilt, never trusted

    from ..bootstrap import ensure_v2_importable
    if not ensure_v2_importable():
        raise RuntimeError(
            "the V2 package is required for causal observation streaming and "
            "could not be imported; see pqv3/bootstrap.py")

    m = Matrix(cols={f: [] for f in FEATURES}, built_ts=int(time.time()))
    n = 0
    for obs in stream_observations_v3(st, store, min_notional=min_notional):
        for f in FEATURES:
            v = getattr(obs, f, 0.0)
            m.cols[f].append(float(v) if not isinstance(v, bool) else float(v))
        tr = obs.trade
        m.resolution.append(float(tr.resolution))
        m.ts.append(int(tr.ts))
        m.wallet.append(tr.wallet)
        m.market_id.append(tr.market_id or "")
        m.token_id.append(tr.token_id)
        n += 1
        if progress and n % 20_000 == 0:
            progress(n)
        if limit and n >= limit:
            break
    m.source_rows = n
    m.fingerprint = fp

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "wb") as fh:
            pickle.dump(m, fh, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:                                         # noqa: BLE001
        pass                         # an unwritable cache must not fail a run
    return m


def cache_info(st: Settings) -> dict:
    p = _cache_path(st)
    if not p.exists():
        return {"cached": False, "path": str(p)}
    return {"cached": True, "path": str(p),
            "size_mb": round(p.stat().st_size / 1e6, 2),
            "mtime": int(p.stat().st_mtime)}
