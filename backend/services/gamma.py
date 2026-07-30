"""
Market metadata service (Gamma API) — async port.

Provides:
  * The settlement/close time of a market (``endDate``) so the copy engine can
    rank qualifying trades by "settles soonest" (Rule 1).
  * The market question / slug for display.
  * Whether a market has closed/resolved, used to settle copied trades.

Two behaviours matter for the "no stale data" requirement:

  * Metadata is cached for 60s, but *closed/resolved* markets are re-checked far
    more often (5s) because that is the transition that decides realized P/L.
  * Concurrent lookups for the same market share a single in-flight request, so
    a burst of positions in one market never fans out into duplicate HTTP calls.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx


@dataclass
class MarketInfo:
    market_id: str
    question: str = ""
    slug: str = ""
    end_ts: Optional[int] = None
    closed: bool = False
    resolved: bool = False
    # Parallel lists: the outcome token ids and, once resolved, their final
    # prices (1.0 for the winning outcome, 0.0 for the losing one).
    token_ids: list = field(default_factory=list)
    outcome_prices: list = field(default_factory=list)

    def resolved_price(self, token_id: str) -> Optional[float]:
        """Final settlement price (1.0/0.0) for ``token_id`` if *decisively*
        resolved. Returns None when the market is closed but not yet resolved to
        a clear winner, so the caller falls back to the last live price rather
        than mis-settling.
        """
        if not self.resolved or not self.token_ids or not self.outcome_prices:
            return None
        prices = self.outcome_prices
        hi, lo = max(prices), min(prices)
        # Decisive only when one outcome ~= 1 and the other ~= 0.
        if hi < 0.99 or lo > 0.01:
            return None
        tid = str(token_id)
        for i, t in enumerate(self.token_ids):
            if str(t) == tid and i < len(prices):
                return 1.0 if prices[i] >= 0.5 else 0.0
        return None


class GammaClient:
    def __init__(self, gamma_host: str, client: httpx.AsyncClient):
        self.host = gamma_host.rstrip("/")
        self._http = client
        self._cache: dict[str, tuple[float, MarketInfo]] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._ttl_open = 60.0
        self._ttl_expiring = 5.0

    def cached(self, condition_id: str) -> Optional[MarketInfo]:
        entry = self._cache.get(condition_id)
        return entry[1] if entry else None

    async def get_market_info(self, condition_id: str,
                              force: bool = False) -> Optional[MarketInfo]:
        """Fetch (and cache) market metadata by condition id."""
        if not condition_id:
            return None
        now = time.time()
        cached = self._cache.get(condition_id)
        if cached and not force:
            age, info = now - cached[0], cached[1]
            # Once a market is at/past its end time we poll aggressively so the
            # resolution (and therefore realized P/L) lands promptly.
            near_end = info.end_ts is not None and now > info.end_ts - 60
            ttl = self._ttl_expiring if near_end else self._ttl_open
            if age < ttl:
                return info

        task = self._inflight.get(condition_id)
        if task is None:
            task = asyncio.create_task(self._fetch(condition_id))
            self._inflight[condition_id] = task
            task.add_done_callback(
                lambda _t, cid=condition_id: self._inflight.pop(cid, None)
            )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            return cached[1] if cached else None

    async def _fetch(self, condition_id: str) -> Optional[MarketInfo]:
        # Gamma's /markets endpoint hides closed markets by default, so a plain
        # lookup returns nothing once a market settles. Try the normal query
        # first, then retry including closed markets so we can read the final
        # resolution for settlement.
        for extra in ({}, {"closed": "true"}):
            info = await self._query(condition_id, extra)
            if info is not None:
                self._cache[condition_id] = (time.time(), info)
                return info
        return None

    async def _query(self, condition_id: str, extra: dict) -> Optional[MarketInfo]:
        params = {"condition_ids": condition_id}
        params.update(extra)
        try:
            resp = await self._http.get(f"{self.host}/markets", params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None
        if isinstance(data, list) and data:
            return _parse_market(data[0])
        if isinstance(data, dict) and data.get("data"):
            return _parse_market(data["data"][0])
        return None

    async def search_markets(self, query: str, limit: int = 20) -> list[dict]:
        """Lightweight market search, used by the UI's market lookup panel."""
        try:
            resp = await self._http.get(
                f"{self.host}/markets",
                params={"active": "true", "closed": "false", "limit": limit,
                        "order": "volume24hr", "ascending": "false"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return []
        items = data if isinstance(data, list) else data.get("data", [])
        q = (query or "").lower().strip()
        out = []
        for m in items:
            info = _parse_market(m)
            if q and q not in info.question.lower() and q not in info.slug.lower():
                continue
            out.append({
                "marketId": info.market_id,
                "question": info.question,
                "slug": info.slug,
                "endTs": info.end_ts,
                "tokenIds": info.token_ids,
            })
        return out


def _parse_market(m: dict) -> MarketInfo:
    return MarketInfo(
        market_id=m.get("conditionId") or m.get("condition_id") or "",
        question=m.get("question") or m.get("title") or "",
        slug=m.get("slug") or "",
        end_ts=iso_to_ts(m.get("endDate") or m.get("end_date_iso")),
        closed=bool(m.get("closed", False)),
        resolved=bool(m.get("resolved", False)) or bool(m.get("closed", False)),
        token_ids=[str(t) for t in _as_list(m.get("clobTokenIds"))],
        outcome_prices=[_to_float(x) for x in _as_list(m.get("outcomePrices"))],
    )


def _as_list(value) -> list:
    """Gamma returns some fields as JSON-encoded strings; normalise to a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def iso_to_ts(iso: Optional[str]) -> Optional[int]:
    """Convert an ISO-8601 timestamp to unix seconds, tolerating variants."""
    if not iso:
        return None
    s = str(iso).strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None
