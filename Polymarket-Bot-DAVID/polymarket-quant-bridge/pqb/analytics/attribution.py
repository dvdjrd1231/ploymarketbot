"""
Where the money actually goes: evidence before optimization.

The operator's profit-improvement spec, distilled: before changing anything,
measure where profit is gained and leaked in the EXISTING system — by price
region, time-to-resolution, holding time, execution quality, and the reasons
trades were refused. Everything here is read-only research over the journal;
nothing gates, sizes, or times a trade. A finding graduates into production
only through the same discipline as a strategy: discovery, out-of-sample
proof, forward confirmation — never because it looks better historically.

Every bucket reports its sample size next to its expectancy, because a
bucket of three trades is an anecdote, not a region.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Optional


def _bucket_rows(rows: list[dict], key_of) -> dict[str, dict]:
    """Group closed trades and compute honest per-bucket economics."""
    buckets: dict[str, dict] = {}
    for row in rows:
        key = key_of(row)
        if key is None:
            continue
        b = buckets.setdefault(key, {"trades": 0, "wins": 0, "netPnl": 0.0,
                                     "holdSeconds": 0.0})
        pnl = float(row.get("realized_pnl") or 0.0)
        b["trades"] += 1
        b["wins"] += 1 if pnl > 0 else 0
        b["netPnl"] += pnl
        b["holdSeconds"] += float(row.get("hold_seconds") or 0.0)
    for b in buckets.values():
        n = b["trades"]
        b["expectancy"] = round(b["netPnl"] / n, 6) if n else 0.0
        b["winRate"] = round(b["wins"] / n, 4) if n else 0.0
        b["avgHoldSeconds"] = round(b["holdSeconds"] / n, 1) if n else 0.0
        b["netPnl"] = round(b["netPnl"], 4)
        del b["holdSeconds"]
    return buckets


def _price_bucket(row: dict) -> Optional[str]:
    price = float(row.get("entry_price") or 0.0)
    if price <= 0:
        return None
    if price < 0.50:
        return "under-50c"
    if price >= 0.995:
        return "99c+"
    lo = int(price * 100) // 10 * 10
    return f"{lo}-{lo + 9}c"


def _hold_bucket(row: dict) -> Optional[str]:
    seconds = float(row.get("hold_seconds") or 0.0)
    if seconds <= 0:
        return None
    if seconds < 60:
        return "under-1m"
    if seconds < 300:
        return "1-5m"
    if seconds < 1800:
        return "5-30m"
    if seconds < 7200:
        return "30m-2h"
    return "over-2h"


def _ttr_bucket(row: dict) -> Optional[str]:
    return str(row.get("ttr_bucket") or "") or None


def _exit_reason(row: dict) -> Optional[str]:
    return str(row.get("exit_reason") or "") or None


_NUMBER = re.compile(r"\d[\d,.]*")


def collapse_reason(reason: str) -> str:
    """'Score 0.31 < 0.55 at ask 0.62' -> 'Score N < N at ask N'.

    Two thousand differently-numbered variants of one sentence are one
    countable reason. Without this, the missed-opportunity log reads as
    noise and the dominant filter hides in plain sight.
    """
    return _NUMBER.sub("N", str(reason or "").strip())[:160]


def report(journal_path: str | Path) -> dict:
    """The full attribution report, from the journal alone. Read-only."""
    path = Path(journal_path)
    if not path.exists():
        return {"available": False, "reason": "no journal yet"}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        closed = [dict(r) for r in conn.execute(
            "SELECT * FROM lifecycles WHERE status='CLOSED'").fetchall()]
        fills = [dict(r) for r in conn.execute(
            "SELECT * FROM executions").fetchall()]
        reasons = [dict(r) for r in conn.execute(
            "SELECT action, reason FROM decisions "
            "WHERE action NOT IN ('BUY','SELL') AND reason != ''").fetchall()]
    finally:
        conn.close()

    out: dict = {"available": True, "closedTrades": len(closed)}

    # -- trade-level economics, bucketed -----------------------------------
    total = sum(float(r.get("realized_pnl") or 0.0) for r in closed)
    wins = sum(1 for r in closed if float(r.get("realized_pnl") or 0.0) > 0)
    out["net"] = {
        "netPnl": round(total, 4),
        "wins": wins,
        "winRate": round(wins / len(closed), 4) if closed else 0.0,
        "expectancy": round(total / len(closed), 6) if closed else 0.0,
    }
    out["byPriceBucket"] = _bucket_rows(closed, _price_bucket)
    out["byHoldTime"] = _bucket_rows(closed, _hold_bucket)
    out["byTimeToResolution"] = _bucket_rows(closed, _ttr_bucket)
    out["byExitReason"] = _bucket_rows(closed, _exit_reason)

    # -- execution quality: intended price vs what the market gave ---------
    slippage = 0.0
    slipped_fills = 0
    fees = 0.0
    filled = partial = failed = 0
    for f in fills:
        fees += float(f.get("fee") or 0.0)
        status = str(f.get("status") or "").upper()
        requested = float(f.get("requested_size") or 0.0)
        got = float(f.get("filled_size") or 0.0)
        if got <= 0:
            failed += 1
            continue
        filled += 1
        if requested > 0 and got < requested * 0.999:
            partial += 1
        limit = float(f.get("limit_price") or 0.0)
        avg = float(f.get("avg_price") or 0.0)
        if limit > 0 and avg > 0:
            side = str(f.get("side") or "BUY").upper()
            worse = (avg - limit) if side.startswith("B") else (limit - avg)
            if worse > 0:
                slippage += worse * got
                slipped_fills += 1
        _ = status
    out["execution"] = {
        "fills": filled, "unfilled": failed, "partialFills": partial,
        "feesPaid": round(fees, 4),
        "slippagePaid": round(slippage, 4),
        "fillsWithSlippage": slipped_fills,
    }

    # -- why the bot said no: the missed-opportunity ledger ----------------
    counted: dict[str, int] = {}
    for row in reasons:
        key = collapse_reason(row.get("reason") or "")
        if key:
            counted[key] = counted.get(key, 0) + 1
    out["skipReasons"] = dict(sorted(counted.items(),
                                     key=lambda kv: -kv[1])[:20])

    # -- leakage: name the biggest drain so effort goes where the money is -
    losses = -sum(min(0.0, float(r.get("realized_pnl") or 0.0))
                  for r in closed)
    leakage = {
        "fees": round(fees, 4),
        "slippage": round(slippage, 4),
        "grossLosses": round(losses, 4),
    }
    out["leakage"] = leakage
    out["largestLeak"] = (max(leakage, key=lambda k: leakage[k])
                         if any(v > 0 for v in leakage.values()) else "")
    return out


def write_report(journal_path: str | Path, out_path: str | Path) -> dict:
    data = report(journal_path)
    Path(out_path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data
