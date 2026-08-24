"""Non-trade wallet activity: REDEEM, MERGE, SPLIT, CONVERSION.

The trade tape comes from the Data API's `/trades` endpoint, which returns
exactly what its name says. That is why every wallet event in this store is a
BUY or a SELL, and why the research layer has had to report the "no REDEEM
before prediction" gate as vacuous and split/merge as unreconstructable.

`/activity` is the wider feed. It carries the same trades plus the lifecycle
events that close a position without selling it — and a redemption is the
difference between "this wallet is still holding" and "this wallet's condition
is over", which is a distinction the whole lifecycle-labelling layer turns on.

Two design points worth stating:

* **A redemption is not a trade and is not stored as one.** It lands in the
  same table (one chronological wallet tape is the point) but carries
  `event_type='REDEEM'` and `side=''`, and every existing query filters on
  `side` — so nothing that reads the tape today changes behaviour, and
  anything that wants redemptions must ask for them.
* **`price` is 0 and `asset` is empty on a redemption.** That is the feed's
  own shape, not missing data: a redemption pays out at settlement across
  whatever the wallet held, so there is no single token or execution price.
  Recording a fabricated price would put an invented number into the same
  column the tape uses for real ones.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

# Everything the feed can emit that is not a trade. TRADE is deliberately
# excluded: the trade tape already has those rows through a path that has been
# de-duplicating them for months, and importing them again through a second
# route would be two sources racing for one natural key.
NON_TRADE_TYPES = ("REDEEM", "MERGE", "SPLIT", "CONVERSION", "REWARD")

PAGE = 500


@dataclass
class ActivityResult:
    wallets: int = 0
    fetched: int = 0
    stored: int = 0
    by_type: dict = field(default_factory=dict)
    errors: int = 0
    error_samples: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"wallets": self.wallets, "fetched": self.fetched,
                "stored": self.stored, "byType": dict(self.by_type),
                "errors": self.errors,
                "errorSamples": self.error_samples[:5]}


def _f(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def fetch_wallet(http, host: str, wallet: str, limit: int = PAGE,
                       max_pages: int = 4) -> list:
    """One wallet's recent activity, paginated, newest first.

    Bounded by `max_pages` rather than run to exhaustion: the feed is deep,
    the research window is not, and an unbounded walk over 70k wallets is a
    rate-limit incident rather than a collection strategy.
    """
    out: list = []
    for page in range(max_pages):
        try:
            response = await http.get(
                f"{host.rstrip('/')}/activity",
                params={"user": wallet, "limit": limit,
                        "offset": page * limit})
            response.raise_for_status()
            payload = response.json()
        except Exception:                                 # noqa: BLE001
            break
        rows = payload if isinstance(payload, list) else (
            payload.get("data") or [])
        rows = [r for r in rows if isinstance(r, dict)]
        out.extend(rows)
        if len(rows) < limit:
            break
    return out


def to_records(rows: Iterable[dict], wallet: str) -> list:
    """Feed rows -> tuples for `IntelStore.record_activity`.

    Trades are dropped here, not filtered downstream, so the caller cannot
    accidentally double-insert the trade tape through this path.
    """
    out = []
    for row in rows:
        event_type = str(row.get("type") or "").upper()
        if event_type not in NON_TRADE_TYPES:
            continue
        out.append((
            str(row.get("proxyWallet") or wallet).lower(),
            int(_f(row.get("timestamp"))),
            str(row.get("conditionId") or ""),
            str(row.get("asset") or ""),
            str(row.get("outcome") or ""),
            "",                                   # side: a redeem has none
            0.0,                                  # price: likewise
            _f(row.get("size")),
            _f(row.get("usdcSize")),
            str(row.get("title") or "")[:200],
            str(row.get("transactionHash") or ""),
            "activity",
            event_type,
        ))
    return out


async def collect(http, store, host: str, wallets: Iterable[str],
                  limit: int = PAGE, max_pages: int = 4,
                  pause_seconds: float = 0.15,
                  progress: Optional[Callable] = None) -> ActivityResult:
    """Backfill non-trade activity for a set of wallets."""
    out = ActivityResult()
    for wallet in wallets:
        out.wallets += 1
        try:
            rows = await fetch_wallet(http, host, wallet, limit, max_pages)
        except Exception as exc:                          # noqa: BLE001
            out.errors += 1
            out.error_samples.append(repr(exc)[:160])
            continue
        out.fetched += len(rows)
        records = to_records(rows, wallet)
        for record in records:
            out.by_type[record[-1]] = out.by_type.get(record[-1], 0) + 1
        if records:
            out.stored += store.record_activity(records)
        if progress and out.wallets % 25 == 0:
            progress(f"  {out.wallets} wallet(s): {out.stored:,} non-trade "
                     f"event(s) stored — {dict(out.by_type)}")
        await asyncio.sleep(pause_seconds)
    return out
