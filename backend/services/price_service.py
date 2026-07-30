"""
Real-time price service.

This is the piece that removes "outdated prices" from the old build. The desktop
app asked the REST API for a price once every three seconds, per position, on
the same thread that redrew the tables — so prices were up to 3s stale *and* the
UI froze while they were fetched.

Here, prices are **pushed**:

  * A single WebSocket to Polymarket's CLOB market channel
    (``wss://ws-subscriptions-clob.polymarket.com/ws/market``) carries the full
    order book for every token we care about. ``book`` snapshots seed it and
    ``price_change`` deltas keep it exact, so the best bid/ask we show is the
    same book the Polymarket site renders.
  * A REST safety net fills in any token the stream has not delivered yet (and
    covers the case where the socket is down), using the batch ``/prices`` and
    ``/midpoints`` endpoints so N positions cost one HTTP call, not N.

Everything is non-blocking: the socket, the fallback poller and the consumers
all live on the event loop, and no consumer ever awaits a network call to read a
price — it reads the in-memory book, which is O(1).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import httpx
import websockets

from ..models import Side


@dataclass
class Book:
    """In-memory order book for one outcome token."""

    token_id: str
    bids: dict[float, float] = field(default_factory=dict)   # price -> size
    asks: dict[float, float] = field(default_factory=dict)
    last_trade: Optional[float] = None
    updated: float = 0.0
    source: str = "none"        # "stream" | "rest"
    tick_size: float = 0.01

    @property
    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    @property
    def midpoint(self) -> Optional[float]:
        bid, ask = self.best_bid, self.best_ask
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return bid if bid is not None else ask

    @property
    def spread(self) -> Optional[float]:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return ask - bid

    def apply_snapshot(self, bids: list, asks: list) -> None:
        self.bids = _levels(bids)
        self.asks = _levels(asks)
        self.updated = time.time()
        self.source = "stream"

    def apply_change(self, price: float, size: float, side: str) -> None:
        if price <= 0:
            return
        # The channel names sides after the side of the book: BUY is the bid
        # side, SELL the ask side.
        book = self.bids if side.upper() in ("BUY", "BID") else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size
        self.updated = time.time()
        self.source = "stream"

    def apply_touch(self, best_bid: Optional[float],
                    best_ask: Optional[float]) -> bool:
        """Reconcile the book against an authoritative touch.

        Every streamed price change states the resulting best bid and ask. A
        delta stream is only ever as good as the messages it saw, so trusting
        that touch repairs whatever was missed across a reconnect — which is
        what stops a stale level from surfacing as a *crossed* book (a bid
        above the ask), a state that would misprice every mark derived from it.
        """
        changed = False
        if best_bid is not None:
            changed |= _trim_to_touch(self.bids, best_bid, keep_below=True)
        if best_ask is not None:
            changed |= _trim_to_touch(self.asks, best_ask, keep_below=False)
        if changed:
            self.updated = time.time()
            self.source = "stream"
        return changed

    def apply_rest(self, bid: Optional[float], ask: Optional[float],
                   mid: Optional[float]) -> None:
        """Replace the book with REST top-of-book values.

        Only the touch is available over REST, which is all any consumer reads.
        This is called for tokens the stream is *not* covering, so replacing is
        right: keeping levels from an older snapshot beside a fresh touch is
        how a book ends up crossed. The source is recorded honestly so the
        dashboard's provenance dot shows amber rather than claiming a stream.
        """
        # REST reports an empty side of the book as "0". That is the absence of
        # a level, not a level priced at zero — storing it would mark a
        # position at a fabricated midpoint and show a 0¢ bid in the UI.
        bid = bid if bid and bid > 0 else None
        ask = ask if ask and ask > 0 else None
        if bid is None and ask is None:
            if mid is None or mid <= 0:
                return
            bid = ask = mid
        self.bids = {bid: 1.0} if bid is not None else {}
        self.asks = {ask: 1.0} if ask is not None else {}
        self.updated = time.time()
        self.source = "rest"

    def to_dict(self) -> dict:
        return {
            "tokenId": self.token_id,
            "bid": self.best_bid,
            "ask": self.best_ask,
            "mid": self.midpoint,
            "spread": self.spread,
            "lastTrade": self.last_trade,
            "updated": self.updated,
            "source": self.source,
        }


def _trim_to_touch(levels: dict[float, float], best: float,
                   keep_below: bool) -> bool:
    """Drop levels the exchange says are gone and ensure ``best`` is present.

    A touch carries no size, so a level learned this way gets a placeholder —
    nothing in the app reads depth, only the best bid and ask.
    """
    if best <= 0:
        # The exchange is telling us this side of the book is empty.
        if not levels:
            return False
        levels.clear()
        return True
    if keep_below:
        stale = [p for p in levels if p > best + 1e-9]
    else:
        stale = [p for p in levels if p < best - 1e-9]
    for price in stale:
        levels.pop(price, None)
    if best not in levels:
        levels[best] = 1.0
        return True
    return bool(stale)


def _levels(rows: Iterable) -> dict[float, float]:
    out: dict[float, float] = {}
    for row in rows or []:
        try:
            if isinstance(row, dict):
                price = float(row.get("price"))
                size = float(row.get("size"))
            else:
                price, size = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if size > 0 and price > 0:
            out[price] = size
    return out


class PriceService:
    """Owns every live price in the application."""

    def __init__(self, clob_host: str, ws_host: str, http: httpx.AsyncClient,
                 use_stream: bool = True, rest_fallback_seconds: float = 4.0,
                 on_update: Optional[Callable[[str], None]] = None):
        self.clob_host = clob_host.rstrip("/")
        self.ws_url = f"{ws_host.rstrip('/')}/market"
        self._http = http
        self._use_stream = use_stream
        self._rest_fallback = max(1.0, rest_fallback_seconds)
        self._on_update = on_update

        self.books: dict[str, Book] = {}
        self._wanted: set[str] = set()
        self._subscribed: frozenset[str] = frozenset()
        self._resubscribe = asyncio.Event()

        self._tasks: list[asyncio.Task] = []
        self._running = False

        # Health, surfaced to the dashboard so the user can see the feed state.
        self.stream_state = "idle"      # idle | connecting | live | retrying | off
        self.stream_error = ""
        self.last_message_ts = 0.0
        self.message_count = 0
        self.reconnects = 0

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._use_stream:
            self._tasks.append(asyncio.create_task(
                self._stream_loop(), name="price-stream"))
        else:
            self.stream_state = "off"
        self._tasks.append(asyncio.create_task(
            self._rest_loop(), name="price-rest"))

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        self.stream_state = "idle"

    # -- subscription --------------------------------------------------------

    def set_rest_fallback(self, seconds: float) -> None:
        """Retune the REST safety net without restarting the service."""
        self._rest_fallback = max(1.0, float(seconds))

    def track(self, token_ids: Iterable[str]) -> None:
        """Ensure ``token_ids`` are streamed. Cheap and idempotent."""
        new = {str(t) for t in token_ids if t}
        added = new - self._wanted
        if not added:
            return
        self._wanted |= added
        for token in added:
            self.books.setdefault(token, Book(token_id=token))
        self._resubscribe.set()

    def untrack(self, token_ids: Iterable[str]) -> None:
        drop = {str(t) for t in token_ids if t} & self._wanted
        if not drop:
            return
        self._wanted -= drop
        self._resubscribe.set()

    def retain_only(self, token_ids: Iterable[str]) -> None:
        """Drop everything not in ``token_ids`` (called when positions close)."""
        keep = {str(t) for t in token_ids if t}
        if keep == self._wanted:
            return
        self._wanted = keep
        for token in list(self.books):
            if token not in keep:
                self.books.pop(token, None)
        self._resubscribe.set()

    # -- reads (always instant, never awaits the network) --------------------

    def book(self, token_id: str) -> Optional[Book]:
        return self.books.get(str(token_id))

    def best_price(self, token_id: str, side: Side) -> Optional[float]:
        """Best executable price for ``side`` (0-1), or None.

        For a BUY we care about the best ASK (what we would pay); for a SELL the
        best BID (what we would receive) — identical semantics to the desktop
        app's REST call, but read from the streamed book.
        """
        b = self.books.get(str(token_id))
        if b is None:
            return None
        return b.best_ask if side == Side.BUY else b.best_bid

    def midpoint(self, token_id: str) -> Optional[float]:
        b = self.books.get(str(token_id))
        return b.midpoint if b else None

    def mark(self, token_id: str, fallback: float = 0.0) -> tuple[float, str]:
        """A robust mark price for an OPEN position, plus its provenance.

        Tries the best bid, then the midpoint, then the last traded price, then
        the caller's fallback. It never returns 0 from an empty/thin book — a
        missing bid must not be shown as a -100% wipe-out. (Actual settlement to
        $0 for a losing outcome is handled by resolution, not here.)
        """
        b = self.books.get(str(token_id))
        if b is not None:
            bid = b.best_bid
            if bid and bid > 0:
                return bid, b.source
            mid = b.midpoint
            if mid and mid > 0:
                return mid, b.source
            if b.last_trade and b.last_trade > 0:
                return b.last_trade, b.source
        return (fallback if fallback and fallback > 0 else 0.0), "last"

    async def price_now(self, token_id: str, side: Side) -> Optional[float]:
        """Best price for ``side``, fetching over REST if the book is cold.

        Used on the entry-price decision path, where being right matters more
        than being instant — if the stream has not delivered this brand-new
        token yet we take the ~100ms REST hit rather than skipping the trade.
        """
        token_id = str(token_id)
        price = self.best_price(token_id, side)
        if price is not None:
            book = self.books[token_id]
            if time.time() - book.updated < 10.0:
                return price
        # Forced: on the entry-price decision path a confirmed touch is worth
        # more than the deeper book we would otherwise keep.
        await self._rest_refresh([token_id], force=True)
        return self.best_price(token_id, side)

    def health(self) -> dict:
        return {
            "state": self.stream_state,
            "error": self.stream_error,
            "tracked": len(self._wanted),
            "subscribed": len(self._subscribed),
            "lastMessageTs": self.last_message_ts,
            "messages": self.message_count,
            "reconnects": self.reconnects,
            "ageSeconds": (time.time() - self.last_message_ts)
            if self.last_message_ts else None,
        }

    def snapshot(self) -> list[dict]:
        return [b.to_dict() for b in self.books.values()]

    # -- websocket -----------------------------------------------------------

    async def _stream_loop(self) -> None:
        backoff = 1.0
        while self._running:
            if not self._wanted:
                # Nothing to watch — wait until a position appears.
                self.stream_state = "idle"
                self._resubscribe.clear()
                try:
                    await asyncio.wait_for(self._resubscribe.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                continue

            assets = sorted(self._wanted)
            self.stream_state = "connecting"
            resubscribing = False
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=15,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=512,
                    open_timeout=15,
                ) as ws:
                    await ws.send(json.dumps(
                        {"assets_ids": assets, "type": "market"}
                    ))
                    self._subscribed = frozenset(assets)
                    self.stream_state = "live"
                    self.stream_error = ""
                    backoff = 1.0
                    self._resubscribe.clear()
                    resubscribing = await self._pump(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stream_error = f"{type(exc).__name__}: {exc}"
            finally:
                self._subscribed = frozenset()

            if not self._running:
                break
            if resubscribing:
                # The watched-token set changed. Reopening immediately is the
                # supported way to change a subscription; it is not a failure,
                # so it neither counts as a reconnect nor waits out a backoff.
                continue
            self.stream_state = "retrying"
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 20.0)

    async def _pump(self, ws) -> bool:
        """Read messages until the socket dies or the token set changes.

        Returns True when it stopped because a resubscription is needed.
        """
        keepalive = asyncio.create_task(self._keepalive(ws))
        watcher = asyncio.create_task(self._resubscribe.wait())
        receiver: Optional[asyncio.Task] = None
        try:
            while self._running:
                receiver = asyncio.create_task(ws.recv())
                done, _ = await asyncio.wait(
                    {receiver, watcher},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if watcher in done:
                    return True
                raw = receiver.result()
                receiver = None
                self.last_message_ts = time.time()
                self._handle_raw(raw)
            return False
        finally:
            # `receiver` is still in flight if this loop was cancelled. Awaiting
            # it here retrieves the close exception the socket teardown raises,
            # which is what keeps shutdown quiet.
            for task in (keepalive, watcher, receiver):
                if task is None:
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _keepalive(self, ws) -> None:
        """Polymarket drops idle sockets; a text PING every 10s keeps it warm."""
        while True:
            await asyncio.sleep(10)
            with contextlib.suppress(Exception):
                await ws.send("PING")

    def _handle_raw(self, raw) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        text = (raw or "").strip()
        if not text or text.upper() in ("PONG", "PING"):
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        events = payload if isinstance(payload, list) else [payload]
        touched: set[str] = set()
        for event in events:
            if isinstance(event, dict):
                touched |= self._handle_event(event)
        self.message_count += len(events)
        if touched and self._on_update is not None:
            for token in touched:
                with contextlib.suppress(Exception):
                    self._on_update(token)

    def _book_for(self, token: str) -> Optional[Book]:
        """The book for a token we are watching, created on first sight."""
        if not token:
            return None
        book = self.books.get(token)
        if book is None:
            if token not in self._wanted:
                return None
            book = self.books.setdefault(token, Book(token_id=token))
        return book

    def _handle_event(self, event: dict) -> set[str]:
        """Apply one market-channel event. Returns the tokens it changed."""
        kind = event.get("event_type") or event.get("eventType") or ""

        # Handled first: a price change is addressed per-change, not per-frame.
        if kind == "price_change":
            return self._handle_price_change(event)

        book = self._book_for(
            str(event.get("asset_id") or event.get("assetId") or ""))
        if book is None:
            return set()
        token = book.token_id

        if kind == "book":
            book.apply_snapshot(event.get("bids") or event.get("buys") or [],
                                event.get("asks") or event.get("sells") or [])
            last = _f(event.get("last_trade_price"))
            if last:
                book.last_trade = last
            tick = _f(event.get("tick_size"))
            if tick:
                book.tick_size = tick
            return {token}

        if kind == "last_trade_price":
            last = _f(event.get("price"))
            if last is not None:
                book.last_trade = last
                book.updated = time.time()
            return {token}

        if kind == "tick_size_change":
            tick = _f(event.get("new_tick_size"))
            if tick:
                book.tick_size = tick
            return set()

        return set()

    def _handle_price_change(self, event: dict) -> set[str]:
        """Apply level deltas from a ``price_change`` frame.

        The live market channel sends one frame per *market*, carrying changes
        for both outcome tokens in ``price_changes`` — each with its own
        ``asset_id`` and the authoritative ``best_bid`` / ``best_ask`` that
        results. There is no ``asset_id`` at the top level, so a change has to
        be routed by its own id: routing the frame as a whole would drop every
        delta (and, worse, could apply one token's levels to its complement).
        Older single-asset shapes are still accepted.
        """
        changes = event.get("price_changes")
        if not isinstance(changes, list):
            changes = event.get("changes")
        default_token = str(event.get("asset_id") or event.get("assetId") or "")
        if not isinstance(changes, list):
            changes = [event]       # oldest shape: one change, inline

        touched: set[str] = set()
        for change in changes:
            if not isinstance(change, dict):
                continue
            book = self._book_for(
                str(change.get("asset_id") or change.get("assetId") or "")
                or default_token)
            if book is None:
                continue
            price = _f(change.get("price"))
            size = _f(change.get("size"))
            if price is not None and size is not None:
                book.apply_change(price, size,
                                  str(change.get("side") or "BUY"))
                touched.add(book.token_id)
            if book.apply_touch(_f(change.get("best_bid")),
                                _f(change.get("best_ask"))):
                touched.add(book.token_id)
        return touched

    # -- REST safety net -----------------------------------------------------

    async def _rest_loop(self) -> None:
        """Top up any token the stream has not covered recently."""
        while self._running:
            try:
                cutoff = time.time() - self._rest_fallback
                stale = [
                    token for token in self._wanted
                    if (b := self.books.get(token)) is None or b.updated < cutoff
                ]
                if stale:
                    await self._rest_refresh(stale[:60])
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(max(0.5, self._rest_fallback / 2.0))

    async def _rest_refresh(self, tokens: list[str],
                            force: bool = False) -> None:
        if not tokens:
            return
        prices = await self._batch_prices(tokens)
        mids = await self._batch_midpoints(tokens)
        applied: list[str] = []
        for token in tokens:
            book = self.books.setdefault(token, Book(token_id=token))
            # A book the stream is still feeding is deeper and fresher than a
            # REST touch; the awaits above take long enough that the stream can
            # have moved on since this token was picked as stale.
            if (not force and book.source == "stream"
                    and time.time() - book.updated < self._rest_fallback):
                continue
            entry = prices.get(token) or {}
            book.apply_rest(entry.get("bid"), entry.get("ask"), mids.get(token))
            applied.append(token)
        if self._on_update is not None:
            for token in applied:
                with contextlib.suppress(Exception):
                    self._on_update(token)

    async def _batch_prices(self, tokens: list[str]) -> dict[str, dict]:
        """``POST /prices`` → {token: {"bid": float|None, "ask": float|None}}.

        The CLOB names sides after the *side of the book*, not after the taker:
        side "BUY" is the buy side and returns the best **bid**, side "SELL"
        returns the best **ask**. Verified against ``GET /book`` for the same
        token — BUY matched the top bid and SELL the top ask.

        Reading this the other way round is not a cosmetic error: it inverts
        the spread, so a buy looks cheaper than it is (defeating the
        entry-price rule) and an open position marks at the ask instead of the
        bid, overstating unrealised P/L.
        """
        body = []
        for token in tokens:
            body.append({"token_id": token, "side": "BUY"})
            body.append({"token_id": token, "side": "SELL"})
        try:
            resp = await self._http.post(f"{self.clob_host}/prices", json=body)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return await self._individual_prices(tokens)
        out: dict[str, dict] = {}
        if isinstance(data, dict):
            for token, sides in data.items():
                if not isinstance(sides, dict):
                    continue
                out[str(token)] = {
                    "bid": _f(sides.get("BUY")),
                    "ask": _f(sides.get("SELL")),
                }
        return out or await self._individual_prices(tokens)

    async def _individual_prices(self, tokens: list[str]) -> dict[str, dict]:
        async def one(token: str) -> tuple[str, dict]:
            async def side(book_side: str) -> Optional[float]:
                try:
                    r = await self._http.get(
                        f"{self.clob_host}/price",
                        params={"token_id": token, "side": book_side},
                    )
                    r.raise_for_status()
                    d = r.json()
                    return _f(d.get("price") if isinstance(d, dict) else d)
                except Exception:
                    return None
            bid, ask = await asyncio.gather(side("BUY"), side("SELL"))
            return token, {"bid": bid, "ask": ask}

        results = await asyncio.gather(*(one(t) for t in tokens),
                                       return_exceptions=True)
        out: dict[str, dict] = {}
        for result in results:
            if isinstance(result, BaseException):
                continue
            token, sides = result
            out[token] = sides
        return out

    async def _batch_midpoints(self, tokens: list[str]) -> dict[str, float]:
        try:
            resp = await self._http.post(
                f"{self.clob_host}/midpoints",
                json=[{"token_id": t} for t in tokens],
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return {}
        out: dict[str, float] = {}
        if isinstance(data, dict):
            for token, mid in data.items():
                value = _f(mid.get("mid") if isinstance(mid, dict) else mid)
                if value is not None:
                    out[str(token)] = value
        return out


def _f(value) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
