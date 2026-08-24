"""Performance metrics - pure numpy (no scipy dependency).

All the Phase 5 ranking metrics are computed here from a trade P&L series and
an equity curve.
"""
from __future__ import annotations

import numpy as np


def _arr(x) -> np.ndarray:
    return np.asarray(x, dtype=float)


def sharpe(trade_pnl, periods_per_year: float = 252.0) -> float:
    r = _arr(trade_pnl)
    if r.size < 2 or r.std(ddof=1) == 0:
        return 0.0
    # per-trade Sharpe annualized by trade count proxy
    return float(r.mean() / r.std(ddof=1) * np.sqrt(min(periods_per_year, r.size)))


def sortino(trade_pnl, periods_per_year: float = 252.0) -> float:
    r = _arr(trade_pnl)
    if r.size < 2:
        return 0.0
    downside = r[r < 0]
    dd = downside.std(ddof=1) if downside.size > 1 else 0.0
    if dd == 0:
        return 0.0
    return float(r.mean() / dd * np.sqrt(min(periods_per_year, r.size)))


def profit_factor(trade_pnl) -> float:
    r = _arr(trade_pnl)
    gains = r[r > 0].sum()
    losses = -r[r < 0].sum()
    if losses == 0:
        return float(gains) if gains > 0 else 0.0
    return float(gains / losses)


def win_rate(trade_pnl) -> float:
    r = _arr(trade_pnl)
    if r.size == 0:
        return 0.0
    return float((r > 0).mean())


def expectancy(trade_pnl) -> float:
    r = _arr(trade_pnl)
    return float(r.mean()) if r.size else 0.0


def max_drawdown(equity) -> tuple[float, float]:
    """Return (max_dd_dollars, max_dd_pct_of_peak)."""
    e = _arr(equity)
    if e.size == 0:
        return 0.0, 0.0
    peak = np.maximum.accumulate(e)
    dd = peak - e
    dd_dollars = float(dd.max())
    with np.errstate(divide="ignore", invalid="ignore"):
        dd_pct = np.where(peak > 0, dd / peak, 0.0)
    return dd_dollars, float(np.nanmax(dd_pct) * 100.0)


def recovery_factor(net_profit: float, max_dd_dollars: float) -> float:
    if max_dd_dollars <= 0:
        return float(net_profit) if net_profit > 0 else 0.0
    return float(net_profit / max_dd_dollars)


def ulcer_index(equity) -> float:
    e = _arr(equity)
    if e.size == 0:
        return 0.0
    peak = np.maximum.accumulate(e)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd_pct = np.where(peak > 0, (e - peak) / peak * 100.0, 0.0)
    return float(np.sqrt(np.mean(dd_pct ** 2)))


def equity_smoothness(equity) -> float:
    """R^2 of equity curve vs a straight line (1 = perfectly smooth growth)."""
    e = _arr(equity)
    if e.size < 3:
        return 0.0
    x = np.arange(e.size, dtype=float)
    a, b = np.polyfit(x, e, 1)
    fit = a * x + b
    ss_res = np.sum((e - fit) ** 2)
    ss_tot = np.sum((e - e.mean()) ** 2)
    if ss_tot == 0:
        return 0.0
    return float(max(0.0, 1.0 - ss_res / ss_tot))


def compute_all(trade_pnl, equity, starting_equity: float) -> dict:
    r = _arr(trade_pnl)
    e = _arr(equity)
    net = float(e[-1] - starting_equity) if e.size else 0.0
    dd_dollars, dd_pct = max_drawdown(e if e.size else [starting_equity])
    return {
        "trades": int(r.size),
        "net_profit": net,
        "sharpe": sharpe(r),
        "sortino": sortino(r),
        "profit_factor": profit_factor(r),
        "win_rate": win_rate(r),
        "avg_trade": expectancy(r),
        "expectancy": expectancy(r),
        "max_drawdown": dd_dollars,
        "max_drawdown_pct": dd_pct,
        "recovery_factor": recovery_factor(net, dd_dollars),
        "ulcer_index": ulcer_index(e if e.size else [starting_equity]),
        "equity_smoothness": equity_smoothness(e if e.size else [starting_equity]),
        "gross_profit": float(r[r > 0].sum()) if r.size else 0.0,
        "gross_loss": float(r[r < 0].sum()) if r.size else 0.0,
    }
