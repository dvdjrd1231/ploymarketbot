"""
Target wallet monitor + on-chain position reader (Polymarket Data API) — async.

Two jobs:

  * ``poll_new_trades`` — the activity feed for the followed wallet, normalised
    into :class:`TargetTrade`. Polymarket exposes no public per-user websocket,
    so short-interval polling of this endpoint remains the supported way to
    detect new positions.
  * ``get_positions`` — what a wallet *actually* holds right now. The engine
    uses this to reconcile our local position book against the exchange, which
    is what keeps "active trades always synchronized" true even if an order
    partially filled or something was traded outside the bot.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx

from ..models import Side, TargetTrade


class DataApiClient:
    def __init__(self, data_api_host: str, client: httpx.AsyncClient):
        self.host = data_api_host.rstrip("/")
        self._http = client
        # Highest activity timestamp we have seen, so we only act on fresh items.
        self._last_seen_ts = 0
        # Trade ids seen at exactly ``_last_seen_ts`` — the feed's resolution is
        # one second, so a strict ">" comparison alone would drop a second trade
        # that landed in the same second as the previous newest one.
        self._seen_at_edge: set[str] = set()

    async def prime(self, wallet: str) -> int:
        """Record current activity as the baseline (so we don't copy history).

        Called once when the engine starts: everything already in the feed is
        marked as "seen" by advancing ``_last_seen_ts`` to the newest item.
        Returns how many historical items were skipped.
        """
        trades = await self.fetch_activity(wallet, limit=100)
        if trades:
            self._last_seen_ts = max(t.timestamp for t in trades)
            self._seen_at_edge = {
                t.trade_id for t in trades if t.timestamp == self._last_seen_ts
            }
        return len(trades)

    async def poll_new_trades(self, wallet: str) -> list[TargetTrade]:
        """Return trades (BUY and SELL) newer than the last-seen timestamp.

        Both sides are returned so the engine can copy new entries (BUY) and
        mirror the target's exits (SELL). Ordered oldest-first. De-duplication
        against previously copied trades is handled by the caller.
        """
        trades = await self.fetch_activity(wallet, limit=100)
        if not trades:
            return []

        fresh = [
            t for t in trades
            if t.timestamp > self._last_seen_ts
            or (t.timestamp == self._last_seen_ts
                and t.trade_id not in self._seen_at_edge)
        ]

        newest = max(t.timestamp for t in trades)
        if newest > self._last_seen_ts:
            self._last_seen_ts = newest
            self._seen_at_edge = {
                t.trade_id for t in trades if t.timestamp == newest
            }
        else:
            self._seen_at_edge.update(
                t.trade_id for t in trades if t.timestamp == self._last_seen_ts
            )

        fresh.sort(key=lambda t: t.timestamp)
        return fresh

    async def get_account_value(self, wallet: str) -> Optional[float]:
        """The wallet's current portfolio value in USDC.

        Used by proportional mode to estimate what fraction of their account a
        given trade represents. Note: only *positions* value is public — a
        trader's idle USDC cash is not visible via this endpoint, so the
        fraction is an approximation of account sizing.
        """
        try:
            resp = await self._http.get(f"{self.host}/value",
                                        params={"user": wallet})
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None
        if isinstance(data, list) and data:
            return _f(data[0].get("value"))
        if isinstance(data, dict):
            return _f(data.get("value"))
        return None

    async def get_positions(self, wallet: str) -> dict[str, dict]:
        """Live on-chain positions for ``wallet``, keyed by token id.

        Each value carries the size actually held plus the marks the Data API
        reports, which the engine uses purely as a cross-check — the streamed
        order book remains the source of truth for pricing.
        """
        try:
            resp = await self._http.get(
                f"{self.host}/positions",
                params={"user": wallet, "sizeThreshold": 0.01, "limit": 500},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return {}
        items = data if isinstance(data, list) else data.get("data", []) or []
        out: dict[str, dict] = {}
        for it in items:
            token = str(it.get("asset") or it.get("tokenId") or "")
            if not token:
                continue
            out[token] = {
                "size": _f(it.get("size")) or 0.0,
                "avgPrice": _f(it.get("avgPrice")) or 0.0,
                "curPrice": _f(it.get("curPrice")) or 0.0,
                "marketId": str(it.get("conditionId") or ""),
                "question": str(it.get("title") or ""),
                "outcome": str(it.get("outcome") or ""),
                "redeemable": bool(it.get("redeemable", False)),
            }
        return out

    # -- HTTP ----------------------------------------------------------------

    async def fetch_activity(self, wallet: str,
                             limit: int = 100) -> list[TargetTrade]:
        try:
            resp = await self._http.get(
                f"{self.host}/activity",
                params={"user": wallet, "limit": limit, "type": "TRADE"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return []

        items = data if isinstance(data, list) else data.get("data", []) or []
        out: list[TargetTrade] = []
        for it in items:
            trade = _parse_activity(it)
            if trade is not None:
                out.append(trade)
        return out


def _parse_activity(it: dict) -> Optional[TargetTrade]:
    """Normalise one activity item into a TargetTrade (BUY/SELL trades only)."""
    try:
        act_type = (it.get("type") or it.get("activityType") or "").upper()
        if act_type and act_type not in ("TRADE", "BUY", "SELL"):
            return None

        side_raw = (it.get("side") or "").upper()
        side = Side.BUY if side_raw in ("BUY", "") else Side.SELL

        price = float(it.get("price", 0) or 0)
        size = float(it.get("size", 0) or 0)
        usdc = float(it.get("usdcSize", it.get("usdc_size", price * size)) or 0)
        ts = int(it.get("timestamp", it.get("time", 0)) or 0)

        token_id = str(
            it.get("asset") or it.get("tokenId") or it.get("token_id") or ""
        )
        market_id = str(
            it.get("conditionId") or it.get("condition_id") or it.get("market") or ""
        )
        trade_id = str(
            it.get("transactionHash") or it.get("id") or f"{token_id}:{ts}:{size}"
        )
        outcome = str(it.get("outcome") or it.get("outcomeName") or "")

        if not token_id or price <= 0 or size <= 0:
            return None

        return TargetTrade(
            trade_id=trade_id,
            market_id=market_id,
            token_id=token_id,
            outcome=outcome,
            side=side,
            price=price,
            size=size,
            usdc_size=usdc if usdc > 0 else price * size,
            timestamp=ts or int(time.time()),
            market_question=str(it.get("title") or it.get("question") or ""),
            slug=str(it.get("slug") or ""),
        )
    except Exception:
        return None


def _f(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
