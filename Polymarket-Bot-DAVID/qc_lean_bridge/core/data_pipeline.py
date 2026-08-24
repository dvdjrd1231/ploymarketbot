"""Phase 1 - Data Integration.

Reusable, config-driven importer for the client's Non-Print Bid/Ask and
Multi-Resolution Line Break CSV exports.

Design goals (from project brief):
  * "swap datasets without code changes" -> everything comes from config.
  * Line break bars are EVENT-DRIVEN (irregular timestamps). We never assume a
    fixed bar interval; alignment is done on the timestamp itself.
  * Multiple CSVs (bid engine, ask engine, resolution 1..100) merge into one
    research frame keyed by timestamp, with column names prefixed by file so
    nothing collides.

Output contract used by every later phase:
    Dataset(
        frame      : DataFrame indexed by DatetimeIndex,
        price      : Series (traded price for P&L),
        metrics    : list[str]  (structural metric column names),
        meta       : dict       (per-column role: bid/ask/price/metric)
    )
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config


@dataclass
class Dataset:
    frame: pd.DataFrame
    price: pd.Series
    metrics: list[str] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "rows": int(len(self.frame)),
            "metric_columns": len(self.metrics),
            "sources": len(self.sources),
            "start": str(self.frame.index.min()) if len(self.frame) else "-",
            "end": str(self.frame.index.max()) if len(self.frame) else "-",
            "price_range": (
                f"{self.price.min():.2f} .. {self.price.max():.2f}"
                if len(self.price) else "-"
            ),
        }


class DataPipeline:
    """Loads and normalizes one or more CSV exports into a single Dataset."""

    def __init__(self, config: Config, logger=None):
        self.cfg = config
        self.log = logger or (lambda msg: None)

    # ---------------------------------------------------------------- public
    def load(self) -> Dataset:
        ds_cfg = self.cfg.section("dataset")
        data_dir = self.cfg.resolve_path("dataset.data_dir")
        files = self._discover_files(data_dir)
        if not files:
            raise FileNotFoundError(
                f"No CSV files found in {data_dir}. "
                f"Point dataset.data_dir at your engine's export folder."
            )

        ts_col = ds_cfg.get("timestamp_column", "timestamp")
        ts_fmt = ds_cfg.get("timestamp_format") or None

        frames = []
        for fp in files:
            df = self._read_one(fp, ts_col, ts_fmt)
            if df is not None and len(df):
                frames.append((Path(fp).stem, df))
                self.log(f"  loaded {Path(fp).name}: {len(df)} rows, "
                         f"{df.shape[1]} cols")

        if not frames:
            raise ValueError("All CSV files were empty or unparseable.")

        merged = self._merge(frames, ds_cfg)
        return self._build_dataset(merged, ds_cfg)

    def validate(self) -> list[str]:
        """Structural validation - returns a list of human-readable issues."""
        issues: list[str] = []
        try:
            ds = self.load()
        except Exception as exc:  # noqa: BLE001
            return [f"FATAL: {exc}"]

        if ds.frame.index.hasnans:
            issues.append("Timestamp index contains NaT values.")
        if not ds.frame.index.is_monotonic_increasing:
            issues.append("Timestamps are not sorted ascending (auto-sorted).")
        if ds.price.isna().any():
            issues.append(f"Price column has {int(ds.price.isna().sum())} NaNs.")
        if len(ds.metrics) == 0:
            issues.append("No structural metric columns detected.")
        dupes = int(ds.frame.index.duplicated().sum())
        if dupes:
            issues.append(f"{dupes} duplicate timestamps (kept last).")
        return issues

    # --------------------------------------------------------------- helpers
    def _discover_files(self, data_dir: Path) -> list[str]:
        if not data_dir.exists():
            return []
        return sorted(glob.glob(str(data_dir / "*.csv")))

    def _read_one(self, fp: str, ts_col: str, ts_fmt) -> pd.DataFrame | None:
        try:
            df = pd.read_csv(fp)
        except Exception as exc:  # noqa: BLE001
            self.log(f"  ! skip {Path(fp).name}: {exc}")
            return None

        # Find timestamp column (case-insensitive tolerance).
        col = self._match_col(df, ts_col)
        if col is None:
            # No timestamp -> synthesize a positional index so the file is still
            # usable; warn loudly because alignment quality suffers.
            self.log(f"  ! {Path(fp).name}: no '{ts_col}' column; using row order")
            df.index = pd.RangeIndex(len(df))
            df.index.name = "_row"
            return df

        ts = pd.to_datetime(df[col], format=ts_fmt, errors="coerce")
        df = df.drop(columns=[col])
        df.index = ts
        df.index.name = "timestamp"
        df = df[~df.index.isna()]
        df = df.sort_index()
        # keep last on duplicate timestamps
        df = df[~df.index.duplicated(keep="last")]
        return df

    @staticmethod
    def _match_col(df: pd.DataFrame, name: str) -> str | None:
        if name in df.columns:
            return name
        lowered = {c.lower(): c for c in df.columns}
        return lowered.get(name.lower())

    def _merge(self, frames: list[tuple[str, pd.DataFrame]], ds_cfg: dict) -> pd.DataFrame:
        how = ds_cfg.get("merge", "outer")
        # Prefix non-unique columns with the file stem to avoid collisions,
        # except when there is a single source (keep names clean).
        prepared = []
        multi = len(frames) > 1
        for stem, df in frames:
            if multi:
                df = df.add_prefix(f"{stem}__")
            prepared.append(df)

        merged = prepared[0]
        for df in prepared[1:]:
            merged = merged.join(df, how=how)

        merged = merged.sort_index()
        if ds_cfg.get("fill_method", "ffill") == "ffill":
            merged = merged.ffill()
        return merged

    def _build_dataset(self, merged: pd.DataFrame, ds_cfg: dict) -> Dataset:
        ignore = set(ds_cfg.get("ignore_columns", []) or [])
        price_col = ds_cfg.get("price_column", "price")

        # Resolve price column across possibly-prefixed names.
        price_series = self._resolve_price(merged, price_col)

        bid_tokens = [t.lower() for t in ds_cfg.get("bid_tokens", ["bid"])]
        ask_tokens = [t.lower() for t in ds_cfg.get("ask_tokens", ["ask"])]

        meta: dict[str, str] = {}
        metrics: list[str] = []
        for col in merged.columns:
            if col in ignore:
                continue
            series = merged[col]
            if not np.issubdtype(series.dtype, np.number):
                # try coercion; if it fails it's a label column -> skip
                coerced = pd.to_numeric(series, errors="coerce")
                if coerced.notna().mean() < 0.5:
                    continue
                merged[col] = coerced
                series = coerced

            low = col.lower()
            if any(t in low for t in bid_tokens):
                meta[col] = "bid"
            elif any(t in low for t in ask_tokens):
                meta[col] = "ask"
            else:
                meta[col] = "metric"
            metrics.append(col)

        # Drop rows with no price and clean up leading NaNs from ffill.
        frame = merged.copy()
        frame["__price__"] = price_series
        frame = frame.dropna(subset=["__price__"])
        # forward/back fill remaining metric NaNs so features are computable
        frame[metrics] = frame[metrics].ffill().bfill()
        price_series = frame.pop("__price__")

        return Dataset(
            frame=frame,
            price=price_series.astype(float),
            metrics=metrics,
            meta=meta,
            sources=list(ds_cfg.get("_sources", [])),
        )

    def _resolve_price(self, merged: pd.DataFrame, price_col: str) -> pd.Series:
        # exact / case-insensitive
        col = self._match_col(merged, price_col)
        if col is not None:
            return pd.to_numeric(merged[col], errors="coerce")
        # prefixed variant: <file>__price
        for c in merged.columns:
            if c.lower().endswith(f"__{price_col.lower()}") or \
               c.lower().split("__")[-1] == price_col.lower():
                return pd.to_numeric(merged[c], errors="coerce")
        # last resort: any column literally containing 'price' / 'close' / 'level'
        for token in ("price", "close", "level", "last"):
            for c in merged.columns:
                if token in c.lower():
                    self.log(f"  price_column '{price_col}' not found; "
                             f"using '{c}'")
                    return pd.to_numeric(merged[c], errors="coerce")
        raise KeyError(
            f"Could not resolve a price column ('{price_col}'). "
            f"Set dataset.price_column to a valid CSV column."
        )
