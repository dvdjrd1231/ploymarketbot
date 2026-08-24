"""Phase 2 - Feature Engineering.

Turns raw structural metrics from the line break engine into a clean matrix of
engineered research features. The brief asks for "as many meaningful features
as possible" and lists ~22 named families; every one is implemented here.

Because the exact CSV schema varies dataset-to-dataset, features are built
*generically* from whatever structural metric columns exist, plus special
handling for Bid/Ask tagged columns and cross-resolution (multi-column) logic.

Every produced column has an entry in `descriptions` so reports and the LEAN
export can explain what each feature means.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .data_pipeline import Dataset


# ------------------------------------------------------------------ helpers
def _rolling_z(s: pd.Series, window: int) -> pd.Series:
    mean = s.rolling(window, min_periods=max(2, window // 3)).mean()
    std = s.rolling(window, min_periods=max(2, window // 3)).std(ddof=0)
    z = (s - mean) / std.replace(0, np.nan)
    return z


def _run_length(sign: pd.Series) -> pd.Series:
    """Consecutive-bar count where the sign is unchanged (persistence)."""
    changed = sign.ne(sign.shift(1))
    grp = changed.cumsum()
    return sign.groupby(grp).cumcount() + 1


def _efficiency_ratio(s: pd.Series, window: int) -> pd.Series:
    """Kaufman efficiency: net change / total path. 1=trend, 0=chop."""
    net = (s - s.shift(window)).abs()
    path = s.diff().abs().rolling(window, min_periods=2).sum()
    return (net / path.replace(0, np.nan)).clip(0, 1)


@dataclass
class FeatureSet:
    frame: pd.DataFrame                      # engineered features (index = data)
    price: pd.Series
    descriptions: dict[str, str] = field(default_factory=dict)
    families: dict[str, list[str]] = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        return list(self.frame.columns)


class FeatureEngineer:
    def __init__(self, dataset: Dataset, fast_windows=(3, 5), slow_windows=(20, 50),
                 logger=None):
        self.ds = dataset
        self.fast = fast_windows
        self.slow = slow_windows
        self.log = logger or (lambda m: None)
        self._feat: dict[str, pd.Series] = {}
        self._desc: dict[str, str] = {}
        self._fam: dict[str, list[str]] = {}

    # ---------------------------------------------------------------- public
    def build(self) -> FeatureSet:
        price = self.ds.price
        metrics = self.ds.metrics
        self.log(f"  engineering features from {len(metrics)} structural metrics")

        self._price_features(price)
        self._per_metric_features(metrics)
        self._bid_ask_features()
        self._cross_resolution_features(metrics)
        self._regime_features(price)

        frame = pd.DataFrame(self._feat, index=price.index)
        # neutralize leading NaNs so every bar is testable
        frame = frame.replace([np.inf, -np.inf], np.nan)
        frame = frame.ffill().fillna(0.0)
        self.log(f"  produced {frame.shape[1]} engineered features")
        return FeatureSet(frame=frame, price=price,
                          descriptions=self._desc, families=self._fam)

    # ------------------------------------------------------------ add helper
    def _add(self, name: str, series: pd.Series, family: str, desc: str):
        self._feat[name] = series
        self._desc[name] = desc
        self._fam.setdefault(family, []).append(name)

    # ---------------------------------------------------------- price family
    def _price_features(self, price: pd.Series):
        for w in self.fast + self.slow:
            self._add(f"px_velocity_{w}", price.diff(w),
                      "Velocity", f"Price change over {w} bars (structural velocity).")
        for w in self.fast + self.slow:
            self._add(f"px_accel_{w}", price.diff(w).diff(w),
                      "Acceleration", f"Change of velocity over {w} bars.")
        for w in self.slow:
            self._add(f"px_momentum_{w}", price.pct_change(w).fillna(0) * 100,
                      "Structural Momentum", f"% momentum over {w} bars.")
            self._add(f"px_efficiency_{w}", _efficiency_ratio(price, w),
                      "Structural Efficiency",
                      f"Kaufman efficiency of price path over {w} bars (1=clean trend).")
            self._add(f"px_stability_{w}",
                      1.0 / (1.0 + price.diff().rolling(w, min_periods=2).std(ddof=0)),
                      "Structural Stability", f"Inverse path volatility over {w} bars.")
        # trend persistence: run length of price-direction
        sign = np.sign(price.diff().fillna(0))
        self._add("px_trend_persistence", _run_length(sign) * sign,
                  "Trend Persistence", "Signed consecutive-bar trend run length.")

    # --------------------------------------------------------- per metric fam
    def _per_metric_features(self, metrics: list[str]):
        frame = self.ds.frame
        w_fast, w_slow = self.fast[0], self.slow[0]
        for col in metrics:
            s = frame[col].astype(float)
            base = _sanitize_name(col)
            # z-score (normalized level, comparable across metrics)
            self._add(f"{base}_z", _rolling_z(s, w_slow), "Structure Alignment",
                      f"Rolling z-score of {col} (how stretched vs its own norm).")
            # velocity / acceleration
            self._add(f"{base}_vel", s.diff(w_fast), "Velocity",
                      f"Velocity of {col} over {w_fast} bars.")
            self._add(f"{base}_accel", s.diff(w_fast).diff(w_fast), "Acceleration",
                      f"Acceleration of {col}.")
            # persistence (run length of same sign of change)
            sign = np.sign(s.diff().fillna(0))
            self._add(f"{base}_persist", _run_length(sign) * sign,
                      "Structure Persistence",
                      f"Signed run length of {col} direction (persistence/phase duration).")
            # compression vs expansion: short range / long range
            rng_s = s.rolling(w_fast, min_periods=2).max() - s.rolling(w_fast, min_periods=2).min()
            rng_l = s.rolling(w_slow, min_periods=2).max() - s.rolling(w_slow, min_periods=2).min()
            comp = (rng_s / rng_l.replace(0, np.nan)).clip(0, 5)
            self._add(f"{base}_compression", 1.0 - comp.clip(0, 1),
                      "Compression Ratio", f"Compression of {col} (short vs long range).")
            self._add(f"{base}_expansion", comp,
                      "Expansion Ratio", f"Expansion of {col} (range widening).")
            # event density: how often the metric changes materially
            changed = (s.diff().abs() > s.diff().abs().rolling(w_slow, min_periods=3).mean())
            self._add(f"{base}_event_density",
                      changed.rolling(w_slow, min_periods=3).mean().fillna(0),
                      "Event Density", f"Fraction of recent bars with a large {col} move.")
            # exhaustion: extreme z that is decelerating
            zc = _rolling_z(s, w_slow)
            self._add(f"{base}_exhaustion",
                      (zc.abs() > 2).astype(float) * -np.sign(s.diff(w_fast)),
                      "Structural Exhaustion",
                      f"{col} at an extreme and turning (exhaustion signal).")

    # ------------------------------------------------------------ bid/ask fam
    def _bid_ask_features(self):
        frame = self.ds.frame
        bid_cols = [c for c, r in self.ds.meta.items() if r == "bid"]
        ask_cols = [c for c, r in self.ds.meta.items() if r == "ask"]
        if not bid_cols or not ask_cols:
            return
        bid = frame[bid_cols].mean(axis=1)
        ask = frame[ask_cols].mean(axis=1)
        total = (bid.abs() + ask.abs()).replace(0, np.nan)
        imbalance = (bid - ask) / total
        self._add("bidask_imbalance", imbalance.fillna(0),
                  "Bid/Ask Imbalance", "Net Bid vs Ask structure imbalance (-1..1).")
        for w in self.slow:
            self._add(f"bidask_imbalance_ma_{w}",
                      imbalance.rolling(w, min_periods=2).mean().fillna(0),
                      "Bid/Ask Imbalance", f"Smoothed Bid/Ask imbalance over {w} bars.")
        self._add("bidask_imbalance_vel", imbalance.diff().fillna(0),
                  "Bid/Ask Imbalance", "Rate of change of Bid/Ask imbalance.")

    # ------------------------------------------- cross-resolution / consensus
    def _cross_resolution_features(self, metrics: list[str]):
        """Resolution Agreement / Disagreement + Multi-Timeframe Consensus.

        Treats every structural metric column as a 'resolution' vote; measures
        how aligned their directions are on each bar.
        """
        frame = self.ds.frame
        if len(metrics) < 2:
            return
        directions = np.sign(frame[metrics].diff().fillna(0.0))
        vote = directions.mean(axis=1)                      # -1..1 net direction
        agreement = directions.apply(
            lambda row: np.abs(row.mean()) if len(row) else 0.0, axis=1)
        self._add("res_consensus", vote,
                  "Multi-Timeframe Consensus",
                  "Net directional vote across all structural resolutions (-1..1).")
        self._add("res_agreement", agreement,
                  "Resolution Agreement",
                  "Magnitude of cross-resolution agreement (0=split, 1=unanimous).")
        self._add("res_disagreement", 1.0 - agreement,
                  "Resolution Disagreement",
                  "Cross-resolution disagreement (1=maximally split).")
        # event clustering: are many resolutions moving at once?
        active = (directions != 0).mean(axis=1)
        for w in self.fast:
            self._add(f"res_event_cluster_{w}",
                      active.rolling(w, min_periods=1).mean(),
                      "Event Clustering",
                      f"Share of resolutions active, smoothed over {w} bars.")
        # event frequency: aggregate change rate
        self._add("res_event_frequency",
                  frame[metrics].diff().abs().sum(axis=1).pipe(
                      lambda x: x / x.rolling(self.slow[0], min_periods=3).mean()
                  ).fillna(0),
                  "Event Frequency",
                  "Normalized aggregate structural change frequency.")

    # ----------------------------------------------------------- regime fam
    def _regime_features(self, price: pd.Series):
        w = self.slow[0]
        # mean reversion characteristic: negative autocorrelation proxy
        ret = price.diff()
        mr = -(ret * ret.shift(1)).rolling(w, min_periods=3).mean()
        self._add("regime_mean_reversion", np.sign(mr) * np.log1p(mr.abs()),
                  "Mean Reversion", "Positive when returns anti-correlate (mean-revert).")
        # breakout characteristic: new extreme vs recent range
        hh = price.rolling(w, min_periods=2).max()
        ll = price.rolling(w, min_periods=2).min()
        pos = (price - ll) / (hh - ll).replace(0, np.nan)
        self._add("regime_breakout", (pos.fillna(0.5) - 0.5) * 2,
                  "Breakout", "Where price sits in its recent range (-1 low..+1 high).")
        # phase transition: sign flip of medium momentum
        mom = price.pct_change(self.fast[0]).fillna(0)
        self._add("regime_phase_transition",
                  (np.sign(mom) != np.sign(mom.shift(1))).astype(float),
                  "Phase Transition", "1 when momentum flips sign (phase change).")


def _sanitize_name(col: str) -> str:
    keep = [c if c.isalnum() else "_" for c in col.lower()]
    name = "".join(keep).strip("_")
    while "__" in name:
        name = name.replace("__", "_")
    return name or "metric"
