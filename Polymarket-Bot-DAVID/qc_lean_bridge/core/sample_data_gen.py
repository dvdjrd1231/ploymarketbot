"""Synthetic Line Break structural data generator (importable module).

Mimics the SHAPE of the client's Non-Print Bid/Ask + Multi-Resolution Line Break
engine output so the whole pipeline is runnable without the real proprietary
data. Replace the exported CSVs with real exports and nothing in the code
changes.

Line break bars are EVENT-DRIVEN: a new row is only emitted when price moves a
threshold, so timestamps are irregular (seconds to minutes apart) - reproduced
here on purpose.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd


def _line_break_series(n: int, seed: int):
    rng = np.random.RandomState(seed)
    price = 5000.0
    prices, times = [], []
    t = datetime(2023, 1, 3, 9, 30, 0)
    regime = 1
    for i in range(n):
        if i % 400 == 0:
            regime *= -1
        drift = 0.06 * regime
        shock = rng.normal(0, 1.1)
        pull = -0.002 * (price - 5000.0)
        price += drift + shock + pull
        prices.append(round(price, 2))
        t = t + timedelta(seconds=int(rng.randint(8, 40)))
        if t.hour >= 16:
            t = t.replace(hour=9, minute=30) + timedelta(days=1)
        times.append(t)
    return pd.DatetimeIndex(times), np.array(prices)


def build(out_dir: str | os.PathLike | None = None, n: int = 15000) -> str:
    """Write 3 synthetic CSV exports to `out_dir` (default: sample_data/exports)."""
    if out_dir is None:
        out_dir = Path(__file__).resolve().parent.parent / "sample_data" / "exports"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    idx, price = _line_break_series(n, seed=42)
    rng = np.random.RandomState(7)
    ret = np.diff(price, prepend=price[0])

    bid = pd.DataFrame(index=idx)
    bid.index.name = "timestamp"
    bid["price"] = price
    bid["bid_structure_state"] = np.sign(ret).cumsum() % 7
    bid["bid_compression"] = np.abs(pd.Series(ret, index=idx).rolling(10).std().bfill().values)
    bid["bid_void_metric"] = np.clip(rng.normal(0, 1, n).cumsum() * 0.1, -20, 20)
    bid["bid_event_count"] = (np.abs(ret) > 1.0).astype(int)
    bid["bid_velocity"] = pd.Series(ret, index=idx).rolling(5).mean().bfill().values
    bid.to_csv(out_dir / "bid_nonprint_structure.csv")

    ask = pd.DataFrame(index=idx)
    ask.index.name = "timestamp"
    ask["ask_structure_state"] = (-np.sign(ret)).cumsum() % 7
    ask["ask_compression"] = np.abs(pd.Series(-ret, index=idx).rolling(10).std().bfill().values)
    ask["ask_void_metric"] = np.clip(rng.normal(0, 1, n).cumsum() * 0.1, -20, 20)
    ask["ask_event_count"] = (np.abs(ret) > 1.2).astype(int)
    ask["ask_velocity"] = pd.Series(-ret, index=idx).rolling(5).mean().bfill().values
    ask.to_csv(out_dir / "ask_nonprint_structure.csv")

    lb = pd.DataFrame(index=idx)
    lb.index.name = "timestamp"
    for res in (1, 5, 10, 25, 50, 100):
        span = max(2, res // 2)
        lb[f"lb_res{res}_dir"] = np.sign(
            pd.Series(price, index=idx).diff(span).bfill().values)
        lb[f"lb_res{res}_persistence"] = (
            pd.Series(price, index=idx).diff(span).bfill().rolling(span).mean().bfill().values)
    lb.to_csv(out_dir / "multi_resolution_linebreak.csv")

    return str(out_dir)
