"""The observation matrix: one causal pass, materialised once, reused by every
hypothesis.

Why this exists. A sweep evaluates thousands of candidate rules. Streaming the
tape once per candidate would be thousands of passes over 878,650 rows. So the
tape is streamed ONCE, through V2's validated causal engine, and flattened into
a column-oriented matrix that every candidate is then evaluated against in
memory.

Reusing `pqv2.substrate.state.stream_observations` rather than reimplementing
it is deliberate and is the single most important reuse decision in V3. That
function enforces the no-look-ahead rule with a heap — a trade's outcome folds
into its wallet's statistics at `settled_ts`, never at `ts` — and
`tests/test_causality.py` asserts it against a case a naive implementation
would pass. Rewriting it would double the amount of causal code that can drift
and halve the number of eyes on it.

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


def build(st: Settings, *, min_notional: float = 1.0, limit: int = 0,
          rebuild: bool = False, progress=None) -> Matrix:
    """Stream the tape once through V2's causal engine and flatten it.

    Cached on disk: this pass takes minutes over the full tape and its output
    is a pure function of (database, feature list, min_notional). Re-running a
    sweep should not re-run the tape.
    """
    path = _cache_path(st)
    if path.exists() and not rebuild:
        try:
            with open(path, "rb") as fh:
                m = pickle.load(fh)
            if isinstance(m, Matrix) and m.n and set(m.cols) == set(FEATURES):
                return m
        except Exception:                                     # noqa: BLE001
            pass                     # a corrupt cache is rebuilt, never trusted

    from ..bootstrap import ensure_v2_importable
    if not ensure_v2_importable():
        raise RuntimeError(
            "the V2 package is required for causal observation streaming and "
            "could not be imported; see pqv3/bootstrap.py")

    from pqv2.config import Settings as V2Settings
    from pqv2.substrate.state import stream_observations

    s2 = V2Settings()
    s2.data_db = st.data_db
    s2.costs.min_price = st.costs.min_price
    s2.costs.max_price = st.costs.max_price

    m = Matrix(cols={f: [] for f in FEATURES}, built_ts=int(time.time()))
    n = 0
    for obs in stream_observations(s2, min_notional=min_notional):
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
