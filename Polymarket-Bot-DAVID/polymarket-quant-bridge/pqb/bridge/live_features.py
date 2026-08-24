"""
Engineering the bridge's features live, using the bridge's own code.

Strategy discovery does not search over the raw columns this project captures.
It searches over what the Quant Bridge's ``FeatureEngineer`` derives from them —
rolling z-scores, velocities, persistence run-lengths, efficiency ratios,
regime measures. A discovered rule therefore names something like
``flow_z_z`` or ``px_velocity_5``, not ``flow_z``.

That makes this module load-bearing rather than an optimisation. Without it the
live engine looks up ``flow_z_z`` in a row that only ever contains the raw
columns, finds nothing, and treats **every discovered rule as stale**. The
bridge would go on running, deciding, and journalling, with its entire
researched view silently switched off and nothing in the logs to say so.

So the same class that built the features during research builds them again
here, over a rolling window of captured history with the current observation
appended. Reimplementing those definitions instead — even carefully — would
reintroduce exactly the drift this exists to prevent: a subtly different
rolling window is not an error, it is a rule that quietly means something else
than it did when it was validated.

Cost is bounded by only ever engineering for tokens actually under
consideration, and by a per-cycle cache.
"""

from __future__ import annotations

from typing import Optional

from ..features import FEATURE_NAMES
from ..logs import Log

# The bridge's slowest rolling window is 50 bars. Below roughly that, its slow
# features are NaN and any rule built on one cannot be evaluated — so a token
# with less history than this is honestly reported as unengineerable rather
# than being engineered into columns full of zeros.
MIN_HISTORY_ROWS = 60

# Rows fed to the engineer. Enough for every window to be well defined, capped
# so the per-token cost does not grow with the length of the session.
WINDOW_ROWS = 240

# Columns that must always be present, whatever the rules reference.
#
# `price` is the series the bridge prices P&L from and derives its global
# features (px_velocity_N, regime_breakout, …) from. The bid/ask-tagged columns
# must ALL be present because the engineer's cross-column features aggregate
# every column carrying those tags — feeding it a subset would leave the name
# `bidask_imbalance_vel` intact while quietly changing what it measures, which
# is worse than being slow.
_ALWAYS = ("price",)


def _is_bid_ask(column: str) -> bool:
    low = column.lower()
    return "bid" in low or "ask" in low


def required_columns(referenced: set[str]) -> set[str]:
    """The raw columns needed to derive this set of engineered features.

    The engineer builds a family of features per raw column (``x_vel``,
    ``x_z``, ``x_persist``, …), so a referenced name maps back to its base by
    longest-prefix match. Names that map to nothing are global or cross-column
    features, which is why price and every bid/ask column are unconditional.
    """
    needed = {c for c in _ALWAYS if c in FEATURE_NAMES}
    needed |= {c for c in FEATURE_NAMES if _is_bid_ask(c)}
    for feature in referenced:
        matches = [c for c in FEATURE_NAMES
                   if feature == c or feature.startswith(f"{c}_")]
        if matches:
            needed.add(max(matches, key=len))
    return needed


class LiveFeatureEngineer:
    """Builds the bridge's engineered feature row for one token, on demand."""

    def __init__(self, store, log: Optional[Log] = None):
        self.store = store
        self.log = log
        self._cache: dict[str, dict[str, float]] = {}
        self._cycle = ""
        self._available: Optional[bool] = None
        self._warned = False
        # Column reduction: which raw columns to feed, and whether it has been
        # proved equivalent to feeding all of them. `None` = feed everything.
        self._columns: Optional[list[str]] = None
        self._verified = False
        self._referenced: set[str] = set()

    def set_referenced_features(self, referenced: set[str]) -> None:
        """Declare which engineered features the loaded rules actually use.

        Feature engineering costs ~5ms per raw column and is almost independent
        of window length, so the full 48-column vector costs ~260ms per token —
        41 seconds a cycle across a 48-token universe, against a 20s cycle.
        Feeding only the columns the rules need brings that back under a
        second. :meth:`_verify_reduction` is what makes it safe to do.
        """
        referenced = set(referenced)
        if referenced == self._referenced:
            return
        self._referenced = referenced
        self._verified = False
        self._columns = (sorted(required_columns(referenced)) if referenced
                         else None)
        self._cache.clear()

    # -- availability --------------------------------------------------------

    def _load(self):
        """Import the bridge's feature layer, once, lazily."""
        if self._available is False:
            return None
        try:
            from ..quant import ensure_on_path
            ensure_on_path()
            import pandas as pd                          # noqa: PLC0415
            from core.data_pipeline import Dataset       # noqa: PLC0415
            from core.features import FeatureEngineer    # noqa: PLC0415
        except Exception as exc:                         # noqa: BLE001
            self._available = False
            if self.log is not None and not self._warned:
                self._warned = True
                self.log.warning(
                    "Quant Bridge feature engineering unavailable; discovered "
                    "rules that reference engineered features cannot be "
                    "evaluated", error=repr(exc))
            return None
        self._available = True
        return pd, Dataset, FeatureEngineer

    @property
    def available(self) -> bool:
        return self._load() is not None

    # -- the row -------------------------------------------------------------

    def begin_cycle(self, cycle_id: str) -> None:
        if cycle_id != self._cycle:
            self._cycle = cycle_id
            self._cache.clear()

    def row_for(self, token_id: str,
                current: dict[str, float]) -> Optional[dict[str, float]]:
        """The engineered row for this token, or ``None`` if it cannot be built.

        ``None`` is deliberately distinct from "a row of zeros": the caller
        needs to be able to tell "this token has no history yet" from "these
        features are genuinely zero", because only the first is a reason to
        skip evaluating a rule rather than to conclude it did not fire.
        """
        token_id = str(token_id)
        cached = self._cache.get(token_id)
        if cached is not None:
            return cached

        loaded = self._load()
        if loaded is None:
            return None
        pd, Dataset, FeatureEngineer = loaded

        history = self.store.research_series(token_id)
        if len(history) < MIN_HISTORY_ROWS:
            return None
        history = history[-WINDOW_ROWS:]

        if not self._verified and self._columns is not None:
            self._verify_reduction(history, current)

        try:
            last = self._engineer(history, current, self._columns)
        except Exception as exc:                         # noqa: BLE001
            if self.log is not None:
                self.log.warning("Live feature engineering failed",
                                 token=token_id[:12], error=repr(exc))
            return None
        if last is None:
            return None

        out = {name: _finite(value) for name, value in last.items()}
        # The raw columns stay addressable too, so a rule discovered on a raw
        # column keeps working alongside one discovered on a derived column.
        out.update({name: float(current.get(name, 0.0))
                    for name in FEATURE_NAMES})
        # Including the non-print engine's structural columns: a rule
        # discovered on np_ columns must find them live, or it can never fire.
        out.update({name: float(value) for name, value in current.items()
                    if name.startswith("np_")})
        self._cache[token_id] = out
        return out

    # -- the engineering itself ---------------------------------------------

    def _engineer(self, history: list[dict], current: dict[str, float],
                  columns: Optional[list[str]]):
        """Run the bridge's FeatureEngineer over history + the live row."""
        loaded = self._load()
        if loaded is None:
            return None
        pd, Dataset, FeatureEngineer = loaded
        names = columns if columns is not None else list(FEATURE_NAMES)

        # The captured series ends at the last capture tick, which may be up to
        # a capture interval old. Appending the live observation is what makes
        # the newest engineered row describe *now*.
        rows = [{name: float(row.get(name, 0.0)) for name in names}
                for row in history]
        rows.append({name: float(current.get(name, 0.0)) for name in names})
        stamps = [row["ts"] for row in history] + [history[-1]["ts"] + 1.0]

        frame = pd.DataFrame(rows, index=pd.to_datetime(stamps, unit="s"))
        frame.index.name = "timestamp"
        # Built exactly as DataPipeline builds it for the research path: one
        # source, so no column prefixing, and the same bid/ask tagging.
        meta = {c: ("bid" if "bid" in c.lower()
                    else "ask" if "ask" in c.lower() else "metric")
                for c in frame.columns}
        dataset = Dataset(frame=frame, price=frame["price"].astype(float),
                          metrics=list(frame.columns), meta=meta)
        return FeatureEngineer(dataset).build().frame.iloc[-1]

    def _verify_reduction(self, history: list[dict],
                          current: dict[str, float]) -> None:
        """Prove the reduced column set gives the referenced features unchanged.

        Run once per strategy load, on the first token engineered. If any
        referenced feature differs, reduction is abandoned for the rest of the
        session and the full column set is used.

        This exists because the failure it guards against is invisible: the
        engineer's cross-column families aggregate whatever bid/ask-tagged
        columns it is given, so a reduced frame can leave every feature *name*
        intact while changing what one of them measures — a validated rule
        quietly meaning something else in production.
        """
        self._verified = True
        try:
            full = self._engineer(history, current, None)
            reduced = self._engineer(history, current, self._columns)
        except Exception:                                # noqa: BLE001
            self._columns = None
            return
        if full is None or reduced is None:
            self._columns = None
            return

        mismatched = []
        for feature in sorted(self._referenced):
            if feature not in full.index:
                continue                                 # stale rule, not ours
            if feature not in reduced.index:
                mismatched.append(feature)
                continue
            a, b = _finite(full[feature]), _finite(reduced[feature])
            if abs(a - b) > max(1e-9, abs(a) * 1e-6):
                mismatched.append(feature)

        if mismatched:
            if self.log is not None:
                self.log.warning(
                    "Column reduction changes referenced features; using the "
                    "full set (slower but correct)",
                    features=",".join(mismatched[:5]))
            self._columns = None
        elif self.log is not None:
            self.log.event("engine.features.reduced",
                           columns=len(self._columns or FEATURE_NAMES),
                           of=len(FEATURE_NAMES),
                           referenced=len(self._referenced))


def _finite(value) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if (out != out or out in (float("inf"), float("-inf"))) else out
