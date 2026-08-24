"""
Live capture for the liquidation-cascade hypothesis. Observes, never trades.

Three concurrent loops, all best-effort and none load-bearing for trading:

* **Liquidation stream** — Binance USD-M futures broadcasts every forced
  order publicly (``<symbol>@forceOrder``); the same connection carries
  ``@aggTrade`` for a rolling BTC price buffer. A forced SELL is a
  liquidated LONG; a forced BUY is a liquidated SHORT.
* **Polymarket snapshot** — polls Gamma for the currently-active 5-minute
  BTC UP/DOWN market and keeps its top-of-book fresh, so each event records
  the exact opportunity that existed the moment it printed.
* **Baseline sampler** — records volatility-matched windows with NO recent
  liquidation, measuring their 60s/180s drift. Without this control group,
  ordinary BTC movement would be indistinguishable from a cascade edge.

Every event spawns a response recorder that samples the price path at
1/5/15/30/60/120/180 seconds, and an outcome recorder that waits for the
5-minute market to expire and scores the hypothetical entry after costs.
Failures degrade to gaps in the record, never to a broken trading loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Optional

from ..logs import Log
from .cascade import (CascadeConfig, CascadeStore, HORIZONS, aggregates,
                      momentum_pct, volatility_pct)

_BUFFER_SECONDS = 420.0


class CascadeMonitor:
    """Owns the capture loops and the store. start()/stop() from the runner."""

    def __init__(self, config: CascadeConfig, store_path, http,
                 gamma_host: str, log: Optional[Log] = None,
                 fee: float = 0.0, assumed_spread: float = 0.01):
        self.cfg = config
        self.store = CascadeStore(store_path)
        self.http = http
        self.gamma_host = gamma_host.rstrip("/")
        self.log = log
        self.fee = float(fee)
        self.assumed_spread = float(assumed_spread)
        self._buffer: list[tuple[float, float]] = []   # (ts, price), ascending
        self._pm: dict = {}                            # active 5m market cache
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._tasks = [
            asyncio.ensure_future(self._stream_loop()),
            asyncio.ensure_future(self._market_loop()),
            asyncio.ensure_future(self._baseline_loop()),
        ]

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.store.close()

    def feature_aggregates(self) -> dict[str, float]:
        """The liq_* columns for this cycle's feature rows."""
        now = time.time()
        return aggregates(self.store.recent_events(now - 300.0), now)

    # -- the exchange stream -------------------------------------------------

    async def _stream_loop(self) -> None:
        if str(self.cfg.source).lower() == "binance":
            await self._binance_loop()
        else:
            await self._okx_loop()

    # OKX: default source. Public channels; `liquidation-orders` broadcasts
    # every forced order on the venue, `trades` keeps the price buffer live.
    # OKX expects a literal "ping" during quiet stretches and answers "pong".
    async def _okx_loop(self) -> None:
        import websockets

        subscribe = json.dumps({"op": "subscribe", "args": [
            {"channel": "trades", "instId": self.cfg.okx_inst_id},
            {"channel": "liquidation-orders", "instType": "SWAP"},
        ]})
        while not self._stopping:
            try:
                async with websockets.connect(self.cfg.okx_host,
                                              open_timeout=15) as ws:
                    await ws.send(subscribe)
                    if self.log:
                        self.log.event("cascade.stream_up", source="okx",
                                       inst=self.cfg.okx_inst_id)
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 20.0)
                        except asyncio.TimeoutError:
                            await ws.send("ping")
                            continue
                        if raw == "pong":
                            continue
                        self._on_okx_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                     # noqa: BLE001
                if self.log:
                    self.log.warning("cascade stream dropped; reconnecting",
                                     error=repr(exc))
                await asyncio.sleep(10.0)

    def _on_okx_message(self, raw) -> None:
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError):
            return
        channel = str((envelope.get("arg") or {}).get("channel") or "")
        rows = envelope.get("data") or []
        if channel == "trades":
            for row in rows:
                price = float(row.get("px") or 0.0)
                if price > 0:
                    self._push_price(time.time(), price)
        elif channel == "liquidation-orders":
            for row in rows:
                if str(row.get("instId") or "") != self.cfg.okx_inst_id:
                    continue
                for detail in row.get("details") or []:
                    price = float(detail.get("bkPx") or 0.0)
                    contracts = float(detail.get("sz") or 0.0)
                    qty = contracts * self.cfg.okx_ct_val   # contracts -> BTC
                    # OKX side is the LIQUIDATION order's side: a sell closes
                    # a long, a buy closes a short — same map as Binance.
                    side = ("long"
                            if str(detail.get("side", "")).lower() == "sell"
                            else "short")
                    self._record_liquidation(side, price, qty)

    # Binance: kept for regions where the futures stream actually serves
    # data; from here it accepts the connection and then goes silent.
    async def _binance_loop(self) -> None:
        import websockets

        symbol = self.cfg.symbol.lower()
        url = (f"{self.cfg.stream_host}/stream?streams="
               f"{symbol}@forceOrder/{symbol}@aggTrade")
        while not self._stopping:
            try:
                async with websockets.connect(url, ping_interval=20,
                                              open_timeout=15) as ws:
                    if self.log:
                        self.log.event("cascade.stream_up", source="binance",
                                       symbol=symbol)
                    async for raw in ws:
                        self._on_binance_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                     # noqa: BLE001
                if self.log:
                    self.log.warning("cascade stream dropped; reconnecting",
                                     error=repr(exc))
                await asyncio.sleep(10.0)

    def _on_binance_message(self, raw) -> None:
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError):
            return
        stream = str(envelope.get("stream") or "")
        data = envelope.get("data") or {}
        if stream.endswith("@aggTrade"):
            price = float(data.get("p") or 0.0)
            if price > 0:
                self._push_price(time.time(), price)
        elif stream.endswith("@forceOrder"):
            order = data.get("o") or {}
            try:
                price = float(order.get("ap") or order.get("p") or 0.0)
                qty = float(order.get("z") or order.get("q") or 0.0)
            except (TypeError, ValueError):
                return
            side = ("long" if str(order.get("S", "")).upper() == "SELL"
                    else "short")
            self._record_liquidation(side, price, qty)

    def _push_price(self, ts: float, price: float) -> None:
        self._buffer.append((ts, price))
        floor = ts - _BUFFER_SECONDS
        while self._buffer and self._buffer[0][0] < floor:
            self._buffer.pop(0)

    def price_now(self) -> float:
        return self._buffer[-1][1] if self._buffer else 0.0

    def _record_liquidation(self, side: str, price: float,
                            qty: float) -> None:
        """One forced order, whichever venue reported it. qty is in BTC."""
        usd = price * qty
        if usd < self.cfg.min_record_usd:
            return                                       # dust only
        now = time.time()
        recent = self.store.recent_events(now - 60.0)
        pm = dict(self._pm) if self._pm else {}
        instrument = (self.cfg.okx_inst_id
                      if str(self.cfg.source).lower() == "okx"
                      else self.cfg.symbol)
        event_id = self.store.record_event(
            ts=now, symbol=instrument, side=side, price=price, qty=qty,
            usd=usd, qualifying=usd >= self.cfg.qualify_liquidation_usd,
            btc_before=self.price_now() or price,
            momentum_30s=momentum_pct(self._buffer, now, 30.0),
            vol_300s=volatility_pct(self._buffer, now),
            events_60s=len(recent) + 1,
            usd_60s=sum(float(e.get("usd") or 0.0) for e in recent) + usd,
            pm=pm)
        if self.log:
            self.log.event("cascade.liquidation", side=side,
                           usd=round(usd, 2), eventId=event_id,
                           market=pm.get("market", ""))
        self._tasks.append(asyncio.ensure_future(
            self._record_responses(event_id, now)))
        if pm.get("endTs"):
            self._tasks.append(asyncio.ensure_future(
                self._record_outcome(event_id, pm)))

    async def _record_responses(self, event_id: int, event_ts: float) -> None:
        for horizon in HORIZONS:
            delay = event_ts + horizon - time.time()
            if delay > 0:
                await asyncio.sleep(delay)
            pm = self._pm or {}
            up_mid = (float(pm.get("upBid") or 0) + float(pm.get("upAsk") or 0)) / 2
            down_mid = (float(pm.get("downBid") or 0)
                        + float(pm.get("downAsk") or 0)) / 2
            self.store.record_response(event_id, horizon, self.price_now(),
                                       up_mid, down_mid)

    async def _record_outcome(self, event_id: int, pm: dict) -> None:
        """Score the hypothetical entry once the 5-minute market expires.

        UP/DOWN resolution follows the market's own rule (close vs open);
        the observed BTC price at expiry versus at the market's open stands
        in until the official resolution is queryable, and the entry price
        was captured AT the event — the opportunity that actually existed.
        """
        open_price = float(pm.get("btcOpen") or 0.0) or self.price_now()
        delay = float(pm.get("endTs") or 0.0) - time.time()
        if delay > 0:
            await asyncio.sleep(min(delay + 2.0, 360.0))
        close_price = self.price_now()
        if close_price <= 0 or open_price <= 0:
            self.store.set_status(event_id, "insufficient_liquidity",
                                  "no price at expiry")
            return
        outcome = "UP" if close_price >= open_price else "DOWN"
        self.store.record_outcome(event_id, outcome, self.fee,
                                  self.assumed_spread)

    # -- the Polymarket 5-minute BTC market ----------------------------------

    async def _market_loop(self) -> None:
        while not self._stopping:
            try:
                await self._refresh_market()
            except asyncio.CancelledError:
                raise
            except Exception:                            # noqa: BLE001
                self._pm = {}
            await asyncio.sleep(10.0)

    async def _refresh_market(self) -> None:
        """The live 5-minute BTC UP/DOWN market, by its deterministic slug.

        Polymarket names these windows ``btc-updown-5m-<start epoch>`` with
        the start aligned to 300s — a direct lookup, where paging by endDate
        surfaced years-old leftovers before the live window ever appeared.
        """
        now = time.time()
        window_start = int(now) // 300 * 300
        response = await self.http.get(
            f"{self.gamma_host}/markets",
            params={"slug": f"btc-updown-5m-{window_start}"})
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            self._pm = {}
            return
        row = rows[0]
        end_ts = _parse_ts(row.get("endDate")) or (window_start + 300.0)
        tokens = row.get("clobTokenIds")
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except ValueError:
                tokens = []
        outcomes = row.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except ValueError:
                outcomes = []
        snapshot = {"market": str(row.get("conditionId") or ""),
                    "question": str(row.get("question") or ""),
                    "endTs": end_ts, "timeLeft": end_ts - now,
                    "btcOpen": self._pm.get("btcOpen") or self.price_now()}
        if self._pm.get("market") != snapshot["market"]:
            snapshot["btcOpen"] = self.price_now()       # new window opens now
        for token, outcome in zip(tokens or [], outcomes or []):
            book = await self._top_of_book(str(token))
            key = "up" if "up" in str(outcome).lower() else "down"
            snapshot[f"{key}Bid"] = book[0]
            snapshot[f"{key}Ask"] = book[1]
        self._pm = snapshot

    async def _top_of_book(self, token_id: str) -> tuple[float, float]:
        try:
            response = await self.http.get(
                "https://clob.polymarket.com/book",
                params={"token_id": token_id})
            response.raise_for_status()
            book = response.json()
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            best_bid = max((float(b["price"]) for b in bids), default=0.0)
            best_ask = min((float(a["price"]) for a in asks), default=0.0)
            return best_bid, best_ask
        except Exception:                                # noqa: BLE001
            return 0.0, 0.0

    # -- the control group ---------------------------------------------------

    async def _baseline_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.cfg.baseline_interval_s)
            try:
                now = time.time()
                if self.store.recent_events(now - 300.0):
                    continue                # an event is nearby; not a control
                start_price = self.price_now()
                if start_price <= 0:
                    continue
                momentum = momentum_pct(self._buffer, now, 30.0)
                vol = volatility_pct(self._buffer, now)
                await asyncio.sleep(60.0)
                move_60 = ((self.price_now() - start_price) / start_price
                           * 100.0)
                await asyncio.sleep(120.0)
                move_180 = ((self.price_now() - start_price) / start_price
                            * 100.0)
                self.store.record_baseline(
                    ts=now, btc_price=start_price, momentum_30s=momentum,
                    vol_300s=vol, move_60s=move_60, move_180s=move_180)
            except asyncio.CancelledError:
                raise
            except Exception:                            # noqa: BLE001
                continue


def _parse_ts(value) -> float:
    if not value:
        return 0.0
    try:
        import datetime as dt
        return dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0
