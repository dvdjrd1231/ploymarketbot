"""
The copy engine — the brain of the application, ported to asyncio.

Every trading rule from the desktop build is preserved verbatim:

  * Mode 1 — Proportional: copy the target's % of account onto your bankroll.
  * Mode 2 — Fixed 50%: one open position at a time; each trade uses 50% of the
    currently available balance → automatic compounding.
  * Rule 1 — Among multiple qualifying trades, only copy the one that settles
    soonest (fast capital recycling).
  * Rule 2 — The copied notional never exceeds the target's dollar amount.
  * Entry-price rule (most important) — only enter if the executable price is
    the SAME or BETTER than the target's entry price; otherwise skip.
  * De-duplication — every target trade id is processed at most once.

What changed is *how* it runs. The desktop version had one 3-second loop doing
everything on a single thread, so a slow HTTP call delayed prices, P/L, balance
and settlement alike — the root cause of the stalls and stale numbers. Here the
work is split into independent asyncio tasks that cannot block each other:

  ``_push_loop``       ~400ms   recompute marks from streamed books, broadcast
  ``_target_loop``     poll_interval   look for new target trades
  ``_account_loop``    5s       wallet balance / buying power
  ``_market_loop``     20s      market metadata + settlement detection
  ``_reconcile_loop``  30s      compare our book against the exchange's

Mark prices come from :class:`PriceService`, which is fed by a WebSocket, so a
position's price is current to the millisecond and reading it costs nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import httpx

from ..applog import AppLogger
from ..db import Database
from ..hub import Hub
from ..models import (
    MODE_FIXED_50, AccountSnapshot, CopiedTrade, OrderRecord, OrderStatus,
    Side, TargetTrade, TradeStatus,
)
from ..settings_store import Settings
from .data_api import DataApiClient
from .gamma import GammaClient, MarketInfo
from .price_service import PriceService
from .trading import (
    OrderResult, PolymarketError, TradingClient, clob_available,
)
from .user_stream import UserStream


def _day_start_ts() -> int:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


class CopyEngine:
    """Owns all live trading state. One instance per run."""

    def __init__(self, settings: Settings, db: Database, hub: Hub,
                 logger: AppLogger, http: httpx.AsyncClient,
                 private_key: str = ""):
        self.settings = settings
        self.db = db
        self.hub = hub
        self.log = logger
        self._private_key = private_key

        self.client: Optional[TradingClient] = None
        self.data = DataApiClient(settings.data_api_host, http)
        self.gamma = GammaClient(settings.gamma_host, http)
        self.prices = PriceService(
            clob_host=settings.clob_host,
            ws_host=settings.clob_ws_host,
            http=http,
            use_stream=settings.use_price_stream,
            rest_fallback_seconds=settings.rest_price_fallback_seconds,
            on_update=self._on_price_update,
        )
        self.user_stream: Optional[UserStream] = None

        # In-memory position registry, keyed by DB id. This — not SQLite — is
        # what the push loop reads, so a live P/L frame never touches the disk.
        self._positions: dict[int, CopiedTrade] = {}
        self._orders: dict[str, OrderRecord] = {}   # by exchange order id

        self._snapshot = AccountSnapshot()
        self._paper_cash = 0.0
        self._target_value: Optional[float] = None

        self.state = "idle"     # idle|connecting|running|stopping|stopped|error
        self.detail = ""
        self.connected = False
        self.started_ts = 0.0

        # Equity history for the portfolio curve. Kept server-side so the chart
        # survives a page reload or a second tab opening — a curve that resets
        # whenever the browser does is not much of a curve.
        self._equity: deque[tuple[float, float, float]] = deque(maxlen=720)
        self._last_equity_sample = 0.0

        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self._dirty = asyncio.Event()
        self._history_dirty = True
        self._orders_dirty = True
        self._copy_lock = asyncio.Lock()

    # ==================================================================
    # lifecycle
    # ==================================================================

    async def start(self) -> None:
        self.state = "connecting"
        self.detail = "Connecting…"
        self._publish_state()

        if not await self._connect():
            self.state = "error"
            self.connected = False
            self._publish_state()
            raise PolymarketError(self.detail or "Could not connect.")

        # Restore any positions left open from a previous run.
        self._positions = {
            t.db_id: t for t in self.db.get_open_trades() if t.db_id is not None
        }
        for order in self.db.get_pending_orders():
            if order.order_id:
                self._orders[order.order_id] = order

        if self.settings.paper_trading:
            self._paper_cash = self._compute_paper_cash()

        await self.prices.start()
        self._retrack_prices()

        # Baseline: treat all existing target activity as already seen so we
        # only copy trades that happen AFTER the bot is started.
        try:
            seen = await self.data.prime(self.settings.target_wallet)
            self.log.info(
                f"Baseline set — {seen} existing trade(s) on the target wallet "
                "ignored. Only new activity from now on will be copied."
            )
        except Exception as exc:
            self.log.warning(f"Could not prime activity baseline: {exc}")

        if self.settings.paper_trading:
            self.log.success(
                f"PAPER TRADING started with ${self._paper_cash:,.2f} virtual "
                f"balance. Following {self.settings.target_wallet} (no real orders)."
            )
        else:
            self.log.success(
                f"Connected as {self.client.address}. "
                f"Following {self.settings.target_wallet}"
            )
            if self.settings.dry_run:
                self.log.warning("DRY-RUN is ON — no real orders will be placed.")

        mode_name = ("Fixed 50%" if self.settings.trading_mode == MODE_FIXED_50
                     else "Proportional")
        self.state = "running"
        self.connected = True
        self.started_ts = time.time()
        self.detail = f"Running — {mode_name} mode"
        self._publish_state()

        self._tasks = [
            asyncio.create_task(self._push_loop(), name="engine-push"),
            asyncio.create_task(self._target_loop(), name="engine-target"),
            asyncio.create_task(self._account_loop(), name="engine-account"),
            asyncio.create_task(self._market_loop(), name="engine-market"),
            asyncio.create_task(self._reconcile_loop(), name="engine-reconcile"),
        ]

    async def stop(self) -> None:
        if self.state in ("stopped", "stopping"):
            return
        self.state = "stopping"
        self.detail = "Stopping…"
        self._publish_state()
        self._stopping.set()

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

        with contextlib.suppress(Exception):
            await self.prices.stop()
        if self.user_stream is not None:
            with contextlib.suppress(Exception):
                await self.user_stream.stop()
            self.user_stream = None

        # Persist the last known marks so a restart shows real numbers instantly.
        for trade in self._positions.values():
            with contextlib.suppress(Exception):
                self.db.update_copied_trade(trade)

        self.state = "stopped"
        self.connected = False
        self.detail = "Stopped"
        self._publish_state()
        self.log.info("Engine stopped.")

    # ==================================================================
    # connection
    # ==================================================================

    async def _connect(self) -> bool:
        # Paper trading needs no wallet/auth — just a virtual balance and the
        # public price feed. This is why it works with no key, no funds, and
        # without the trading-permission side of Polymarket.
        if self.settings.paper_trading:
            self._paper_cash = max(0.0, float(self.settings.paper_starting_balance))
            self.client = None
            return True

        ok, err = clob_available()
        if not ok:
            self.detail = (
                f"Trading library not available ({err}). Run: "
                "pip install -r requirements.txt"
            )
            self.log.error(self.detail)
            return False

        try:
            self.client = await TradingClient(
                host=self.settings.clob_host,
                chain_id=self.settings.chain_id,
                private_key=self._private_key,
                signature_type=self.settings.signature_type,
                funder_address=self.settings.funder_address,
            ).connect()
        except PolymarketError as exc:
            self.detail = str(exc)
            self.log.error(str(exc))
            return False

        auth = self.client.ws_auth()
        if auth:
            self.user_stream = UserStream(
                self.settings.clob_ws_host, auth, self._on_user_event
            )
            await self.user_stream.start()
        return True

    async def _reconnect(self) -> None:
        """Retry the exchange connection with backoff until healthy."""
        if self.settings.paper_trading:
            return
        self.connected = False
        self.detail = "Reconnecting…"
        self._publish_state()
        backoff = 5
        while not self._stopping.is_set():
            self.log.info(f"Reconnecting in {backoff}s…")
            await self._sleep(backoff)
            if self._stopping.is_set():
                return
            if self.client is not None and await self.client.check_connection():
                self.connected = True
                self.detail = "Running"
                self._publish_state()
                self.log.success("Reconnected.")
                return
            if await self._connect():
                self.connected = True
                self.detail = "Running"
                self._publish_state()
                self.log.success("Reconnected.")
                return
            backoff = min(backoff * 2, 60)

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately if a stop is requested."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    # ==================================================================
    # price routing
    # ==================================================================

    def _on_price_update(self, token_id: str) -> None:
        """Called by PriceService whenever a tracked book changes."""
        self._dirty.set()

    def _retrack_prices(self) -> None:
        tokens = {t.token_id for t in self._positions.values() if t.token_id}
        self.prices.retain_only(tokens)
        if self.user_stream is not None:
            self.user_stream.track_markets(
                {t.market_id for t in self._positions.values() if t.market_id}
            )

    async def _best_price(self, token_id: str, side: Side) -> Optional[float]:
        """Executable price, refreshed over REST if the book is cold."""
        return await self.prices.price_now(token_id, side)

    def _mark(self, trade: CopiedTrade) -> tuple[float, str]:
        return self.prices.mark(
            trade.token_id, trade.current_price or trade.my_price
        )

    # ==================================================================
    # loops
    # ==================================================================

    async def _push_loop(self) -> None:
        """Recompute live marks and broadcast. The UI's heartbeat."""
        floor = 0.12   # never push faster than this, however busy the book is
        while not self._stopping.is_set():
            # Re-read each tick so the cadence sliders in Settings take effect
            # immediately, without restarting the engine.
            interval = max(0.1, self.settings.push_interval_ms / 1000.0)
            self.prices.set_rest_fallback(self.settings.rest_price_fallback_seconds)
            try:
                self._apply_marks()
                stats = self._stats_payload()
                self.hub.publish("positions", self._positions_payload())
                self.hub.publish("stats", stats)
                self.hub.publish("feeds", self._feeds_payload())
                if self._sample_equity(stats):
                    self.hub.publish("equity", self.equity_payload())
                if self._history_dirty:
                    self._history_dirty = False
                    self.hub.publish("history", self._history_payload())
                if self._orders_dirty:
                    self._orders_dirty = False
                    self.hub.publish("orders", self._orders_payload())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error(f"Push error: {exc}")

            await asyncio.sleep(floor)
            # Wake early when a price actually moved; otherwise tick on schedule.
            self._dirty.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._dirty.wait(),
                                       timeout=max(0.01, interval - floor))

    def _sample_equity(self, stats: dict) -> bool:
        """Append an equity point every ~5s. True when one was added."""
        now = time.time()
        if now - self._last_equity_sample < 5.0:
            return False
        cash = (self._paper_cash if self.settings.paper_trading
                else self._snapshot.usdc_balance)
        equity = cash + stats["openValue"]
        if equity <= 0 and not self._positions:
            return False
        self._last_equity_sample = now
        self._equity.append((now, equity, stats["portfolioPnl"]))
        return True

    def equity_payload(self) -> list[dict]:
        return [{"t": t, "equity": e, "pnl": p} for t, e, p in self._equity]

    def _apply_marks(self) -> None:
        for trade in self._positions.values():
            price, source = self._mark(trade)
            if price > 0:
                trade.current_price = price
                trade.price_source = source
                book = self.prices.book(trade.token_id)
                trade.price_ts = book.updated if book else 0.0

    async def _target_loop(self) -> None:
        """Poll the target wallet for new trades to copy."""
        while not self._stopping.is_set():
            await self._sleep(max(5, self.settings.poll_interval_seconds))
            if self._stopping.is_set():
                return
            try:
                async with self._copy_lock:
                    await self._process_new_target_trades()
            except asyncio.CancelledError:
                raise
            except PolymarketError as exc:
                self.log.error(f"Exchange error: {exc}")
                await self._reconnect()
            except Exception as exc:
                self.log.error(f"Target poll error: {exc}")

    async def _account_loop(self) -> None:
        """Keep the wallet balance / buying power fresh."""
        while not self._stopping.is_set():
            try:
                await self._refresh_account()
            except asyncio.CancelledError:
                raise
            except PolymarketError as exc:
                self.log.error(f"Balance error: {exc}")
                await self._reconnect()
            except Exception as exc:
                self.log.error(f"Account refresh error: {exc}")
            await self._sleep(max(2, self.settings.balance_refresh_seconds))

    async def _market_loop(self) -> None:
        """Refresh market metadata and settle anything that has resolved."""
        while not self._stopping.is_set():
            try:
                await self._update_market_state()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error(f"Market refresh error: {exc}")
            await self._sleep(max(5, self.settings.market_refresh_seconds))

    async def _reconcile_loop(self) -> None:
        """Compare our position/order book against the exchange's."""
        while not self._stopping.is_set():
            await self._sleep(max(10, self.settings.reconcile_seconds))
            if self._stopping.is_set():
                return
            try:
                await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error(f"Reconcile error: {exc}")

    # ==================================================================
    # account
    # ==================================================================

    async def _refresh_account(self) -> None:
        open_value = sum(t.market_value for t in self._positions.values())
        if self.settings.paper_trading:
            balance = self._paper_cash
            address = "PAPER (simulated)"
            label = "Paper trading — simulated funds"
        else:
            balance = await self.client.get_usdc_balance()
            address = self.client.address
            label = "Live wallet"
        self._snapshot = AccountSnapshot(
            connected_address=address,
            usdc_balance=balance,
            total_position_value=open_value,
            buying_power=balance,           # free cash == buying power
            connected=self.connected,
            paper=self.settings.paper_trading,
            account_label=label,
            updated_ts=time.time(),
        )
        self.hub.publish("account", self._snapshot.to_dict())

    def _available_balance(self) -> float:
        # In paper mode use the live virtual cash so several copies placed in
        # the same batch each see the balance already reduced.
        if self.settings.paper_trading:
            return max(0.0, self._paper_cash)
        return max(0.0, self._snapshot.usdc_balance)

    def _account_equity(self) -> float:
        """Total bankroll = free cash + current value of open positions.

        Proportional sizing is taken against this (not just free cash) so each
        copy is the same fraction of *your* bankroll as the target's trade was
        of theirs — a faithful mirror rather than a shrinking-cash approximation.
        """
        positions_value = sum(t.market_value for t in self._positions.values())
        cash = (self._paper_cash if self.settings.paper_trading
                else self._snapshot.usdc_balance)
        return max(0.0, cash + positions_value)

    def _compute_paper_cash(self) -> float:
        """Virtual cash implied by the trades currently on the books.

        Cash flow is: start, minus the cost of every buy, plus the proceeds of
        every settlement. That reduces to
        ``start - open_cost + realized_pnl_of_closed``, which reconstructs the
        balance exactly when a previous session is restored.
        """
        start = max(0.0, float(self.settings.paper_starting_balance))
        if not self.settings.restore_previous_session:
            return start
        open_cost = sum(t.my_usdc for t in self._positions.values())
        realized = sum(
            t.realized_pnl for t in self.db.get_closed_trades(limit=100000)
            if t.status == TradeStatus.CLOSED
        )
        return max(0.0, start - open_cost + realized)

    # ==================================================================
    # settlement / open-position maintenance
    # ==================================================================

    async def _update_market_state(self) -> None:
        if not self._positions:
            return
        market_ids = {t.market_id for t in self._positions.values() if t.market_id}
        infos = await asyncio.gather(
            *(self.gamma.get_market_info(mid) for mid in market_ids),
            return_exceptions=True,
        )
        by_id: dict[str, MarketInfo] = {}
        for mid, info in zip(market_ids, infos):
            if isinstance(info, MarketInfo):
                by_id[mid] = info

        now = time.time()
        for trade in list(self._positions.values()):
            info = by_id.get(trade.market_id)
            if info is not None:
                # Keep the countdown and title honest even if they were missing
                # or shifted after the position was opened.
                if info.end_ts and info.end_ts != trade.market_end_ts:
                    trade.market_end_ts = info.end_ts
                    self.db.update_copied_trade(trade)
                if info.question and not trade.market_question:
                    trade.market_question = info.question

            settled = False
            if info is not None and (info.resolved or info.closed):
                settled = True
            elif trade.market_end_ts and now > trade.market_end_ts + 3600:
                # Grace period past the listed end time → treat as settled.
                settled = True

            if settled:
                await self._settle_trade(trade, info)

    async def _settle_trade(self, trade: CopiedTrade,
                            info: Optional[MarketInfo] = None) -> None:
        # Prefer the *actual* market resolution (winning outcome = $1, losing =
        # $0) so realized P/L is accurate. Fall back to the last live price only
        # when the resolution is not yet available.
        final_price = None
        resolved = False
        if info is not None:
            rp = info.resolved_price(trade.token_id)
            if rp is not None:
                final_price = rp
                resolved = True
        if final_price is None:
            final_price = trade.current_price if trade.current_price else trade.my_price

        trade.realized_pnl = (final_price - trade.my_price) * trade.my_size
        if self.settings.paper_trading:
            self._paper_cash += final_price * trade.my_size
        trade.current_price = final_price
        trade.status = TradeStatus.CLOSED
        trade.closed_ts = int(time.time())
        self.db.update_copied_trade(trade)
        self._retire(trade)

        outcome_note = ""
        if resolved:
            outcome_note = (" — WON (resolved $1.00)" if final_price >= 0.5
                            else " — LOST (resolved $0.00)")
        self.log.success(
            f"Position settled: {trade.market_question or trade.token_id}"
            f"{outcome_note} | entry {trade.my_price:.3f} vs target "
            f"{trade.target_price:.3f} | realized P/L ≈ "
            f"${trade.realized_pnl:,.2f}. Capital freed for reuse."
        )

    def _retire(self, trade: CopiedTrade) -> None:
        """Move a position out of the live registry and into history."""
        if trade.db_id is not None:
            self._positions.pop(trade.db_id, None)
        self._retrack_prices()
        self._history_dirty = True
        self._dirty.set()

    # ==================================================================
    # new target trades  (rules ported verbatim)
    # ==================================================================

    async def _process_new_target_trades(self) -> None:
        fresh = await self.data.poll_new_trades(self.settings.target_wallet)
        if not fresh:
            return

        # Skip anything we've already processed (de-duplication rule).
        fresh = [t for t in fresh if not self.db.is_processed(t.trade_id)]
        if not fresh:
            return

        # Split by side: mirror the target's EXITS (sells) first, then evaluate
        # new ENTRIES (buys).
        sells = [t for t in fresh if t.side == Side.SELL]
        buys = [t for t in fresh if t.side == Side.BUY]

        for s in sells:
            self.db.mark_processed(s.trade_id)
            if self.settings.copy_exits:
                await self._handle_target_exit(s)

        fresh = buys
        if not fresh:
            return

        # Enrich with settlement time so we can honour Rule 1.
        infos = await asyncio.gather(
            *(self.gamma.get_market_info(t.market_id) for t in fresh),
            return_exceptions=True,
        )
        for t, info in zip(fresh, infos):
            if isinstance(info, MarketInfo):
                t.market_end_ts = info.end_ts
                if not t.market_question:
                    t.market_question = info.question

        # Min-expiry filter: never enter a market that has already expired / is
        # settling (or whose end time is unknown). This guarantees every copied
        # position opens with a real countdown, not an instant "settling".
        now_ts = time.time()
        buffer = max(0, self.settings.min_seconds_to_expiry)
        with_time = []
        for t in fresh:
            if not t.market_end_ts:
                self.db.mark_processed(t.trade_id)
                self._record_skip(t, "Market end time unknown — not entered.")
                self.log.info(
                    f"Skipped '{t.market_question or t.token_id}': market end "
                    "time unknown, cannot verify it isn't already settling."
                )
            elif t.market_end_ts <= now_ts + buffer:
                self.db.mark_processed(t.trade_id)
                self._record_skip(
                    t, "Market already expired / settling — not entered.")
                self.log.info(
                    f"Skipped '{t.market_question or t.token_id}': market is "
                    "already expiring/settling (no time left to enter)."
                )
            else:
                with_time.append(t)
        fresh = with_time
        if not fresh:
            return

        # Max-expiry filter: drop trades that settle further out than the user's
        # configured limit (0 = no limit).
        max_days = self.settings.max_expiry_days
        if max_days and max_days > 0:
            cutoff = time.time() + max_days * 86400
            kept = []
            for t in fresh:
                if t.market_end_ts and t.market_end_ts > cutoff:
                    self.db.mark_processed(t.trade_id)
                    self._record_skip(
                        t, f"Settles beyond the {max_days}-day max expiry.")
                    self.log.info(
                        f"Skipped '{t.market_question or t.token_id}': settles "
                        f"beyond the {max_days}-day max-expiry limit."
                    )
                else:
                    kept.append(t)
            fresh = kept
            if not fresh:
                return

        # Rank by soonest-settling (trades with no known end time last).
        fresh.sort(key=lambda t: (t.market_end_ts is None,
                                  t.market_end_ts or 1 << 62))

        if self.settings.trading_mode == MODE_FIXED_50:
            # Fixed-50: only ONE open position at a time, and (Rule 1) copy only
            # the soonest-settling qualifying trade.
            if len(self._positions) > 0:
                for t in fresh:
                    self.db.mark_processed(t.trade_id)
                self.log.info(
                    f"{len(fresh)} new target trade(s) ignored — a position is "
                    "already open (Fixed 50% mode allows one at a time)."
                )
                return
            acted = False
            for t in fresh:
                self.db.mark_processed(t.trade_id)
                if acted:
                    self.log.info(
                        f"Skipped '{t.market_question or t.token_id}' — copying "
                        "the sooner-settling trade instead (Rule 1, Fixed 50%)."
                    )
                    self._record_skip(
                        t, "Not the soonest-settling qualifying trade.")
                    continue
                if await self._evaluate_and_copy(t):
                    acted = True
            return

        # Proportional mode: mirror ALL qualifying trades (can hold many
        # positions at once), each sized as the target's % of account applied to
        # your balance. Evaluated soonest-settling first so short-dated trades
        # get funded before capital is used up.
        for t in fresh:
            self.db.mark_processed(t.trade_id)
            await self._evaluate_and_copy(t)

    async def _evaluate_and_copy(self, target: TargetTrade) -> bool:
        """Apply entry-price, sizing and Rule 2, then place the order.

        Returns True if an order was placed (or simulated), else False.
        """
        # 1) Entry-price rule — most important. Get the price we would pay now.
        exec_price = await self._best_price(target.token_id, Side.BUY)
        if exec_price is None:
            self._record_skip(target,
                              "No live price available — cannot verify entry.")
            self.log.warning(
                f"Skipped '{target.market_question}': no order-book price to "
                "verify entry."
            )
            return False

        if exec_price > target.price + 1e-9:
            self._record_skip(
                target,
                f"Price moved to {exec_price:.3f} > target entry "
                f"{target.price:.3f}.",
            )
            self.log.info(
                f"Skipped '{target.market_question}': market at "
                f"{exec_price:.3f}¢ is worse than target entry "
                f"{target.price:.3f}¢ (entry-price rule)."
            )
            return False

        # 2) Position sizing per the selected mode.
        available = self._available_balance()
        desired_usdc = await self._desired_usdc(target, available)

        # 3) Sizing limits.
        min_order = self.settings.min_order_usdc
        cap = target.usdc_size * self.settings.max_copy_fraction_of_target  # Rule 2

        # Floor tiny proportional sizes UP to the user's minimum so the account
        # still takes the position (e.g. a 0.1%-of-a-huge-account trade would be
        # a few cents otherwise). Then never exceed the target's dollar amount
        # (Rule 2) or your available cash.
        desired_usdc = max(desired_usdc, min_order)
        desired_usdc = min(desired_usdc, cap, available)

        if desired_usdc < min_order - 1e-9:
            self._record_skip(
                target,
                f"Cannot place the ${min_order:.2f} minimum (target size "
                f"${cap:.2f}, available ${available:.2f}).",
            )
            self.log.info(
                f"Skipped '{target.market_question}': can't meet the "
                f"${min_order:.2f} minimum within target-size/balance limits."
            )
            return False

        # Size shares using the target price (the max we'll pay). Because the
        # order fills at target-or-better, actual spend <= desired <= target.
        limit_price = round(target.price, 3)
        size = round(desired_usdc / limit_price, 2)
        if size <= 0:
            self._record_skip(target, "Computed share size rounded to zero.")
            return False

        return await self._place_copy(target, limit_price, exec_price, size)

    async def _desired_usdc(self, target: TargetTrade, available: float) -> float:
        if self.settings.trading_mode == MODE_FIXED_50:
            # Mode 2: exactly 50% of currently available balance.
            return available * 0.5

        # Mode 1 — Proportional: mirror the same fraction of account the target
        # used, applied to YOUR total bankroll (cash + open positions).
        target_account = await self.data.get_account_value(
            self.settings.target_wallet)
        self._target_value = target_account
        if target_account and target_account > 0:
            fraction = min(max(target.usdc_size / target_account, 0.0), 1.0)
            equity = self._account_equity()
            self.log.info(
                f"Proportional: target used ~{fraction * 100:.1f}% of account "
                f"(${target.usdc_size:.2f} of ${target_account:.2f}); mirroring "
                f"that on your ${equity:.2f} bankroll."
            )
            return equity * fraction
        # Fallback if the target's account value is unavailable: mirror
        # notional, capped later by Rule 2 and available balance.
        self.log.warning(
            "Could not read target account value; falling back to matching "
            "notional."
        )
        return min(target.usdc_size, available)

    async def _place_copy(self, target: TargetTrade, limit_price: float,
                          exec_price: float, size: float) -> bool:
        record = CopiedTrade(
            target_trade_id=target.trade_id,
            market_id=target.market_id,
            token_id=target.token_id,
            outcome=target.outcome,
            market_question=target.market_question,
            side=Side.BUY,
            mode=self.settings.trading_mode,
            target_price=target.price,
            target_usdc=target.usdc_size,
            market_end_ts=target.market_end_ts,
            opened_ts=int(time.time()),
        )

        simulated = self.settings.paper_trading or self.settings.dry_run
        if simulated:
            tag = "PAPER" if self.settings.paper_trading else "DRY-RUN"
            record.status = TradeStatus.OPEN
            record.my_price = exec_price
            record.my_size = size
            record.my_usdc = size * exec_price
            record.current_price = exec_price
            record.order_id = tag
            self.db.insert_copied_trade(record)
            self._adopt(record)
            self._track_order(record, OrderRecord(
                order_id=f"{tag}-{record.db_id}",
                token_id=record.token_id, market_id=record.market_id,
                market_question=record.market_question, outcome=record.outcome,
                side=Side.BUY, price=exec_price, size=size, filled_size=size,
                status=OrderStatus.FILLED, trade_db_id=record.db_id,
            ))
            if self.settings.paper_trading:
                # Deduct simulated cost so the virtual balance compounds like
                # the real thing once the position settles.
                self._paper_cash = max(0.0, self._paper_cash - record.my_usdc)
                self.log.success(
                    f"[PAPER] BUY {size:.2f} @ {exec_price:.3f} (target entered "
                    f"@ {target.price:.3f} — same or better ✓) "
                    f"${record.my_usdc:.2f} on '{target.market_question}'. "
                    f"Virtual cash left: ${self._paper_cash:,.2f}."
                )
            else:
                self.log.success(
                    f"[DRY-RUN] Would BUY {size:.2f} @ {exec_price:.3f} (target "
                    f"entered @ {target.price:.3f} — same or better ✓) "
                    f"${record.my_usdc:.2f} on '{target.market_question}'."
                )
            return True

        self.log.info(
            f"Placing BUY {size:.2f} @ limit {limit_price:.3f} on "
            f"'{target.market_question}'…"
        )
        pending = OrderRecord(
            order_id="", token_id=target.token_id, market_id=target.market_id,
            market_question=target.market_question, outcome=target.outcome,
            side=Side.BUY, price=limit_price, size=size,
            status=OrderStatus.PENDING,
        )
        self.db.insert_order(pending)
        self._orders_dirty = True
        self._dirty.set()

        result: OrderResult = await self.client.place_limit_order(
            token_id=target.token_id, side=Side.BUY, price=limit_price,
            size=size, order_type="FAK",
        )

        pending.order_id = result.order_id or pending.order_id
        pending.filled_size = result.filled_size
        pending.error = result.error

        if result.success and result.filled_size > 0:
            record.status = TradeStatus.OPEN
            record.my_size = result.filled_size
            record.my_price = result.avg_price or limit_price
            record.my_usdc = record.my_size * record.my_price
            record.current_price = record.my_price
            record.order_id = result.order_id
            self.db.insert_copied_trade(record)
            self._adopt(record)

            pending.status = (OrderStatus.FILLED
                              if result.filled_size >= size - 1e-9
                              else OrderStatus.MATCHED)
            pending.trade_db_id = record.db_id
            self._track_order(record, pending)
            self.log.success(
                f"Copied trade filled: {record.my_size:.2f} shares @ "
                f"{record.my_price:.3f} (target entered @ {target.price:.3f} — "
                f"same or better ✓) ${record.my_usdc:.2f} — order "
                f"{result.order_id}."
            )
            return True

        if result.success and result.filled_size == 0:
            record.status = TradeStatus.SKIPPED
            record.reason = "Order did not fill at target-or-better price."
            self.db.insert_copied_trade(record)
            pending.status = OrderStatus.CANCELED
            self._track_order(record, pending)
            self._history_dirty = True
            self.log.info(
                f"No fill for '{target.market_question}' at {limit_price:.3f} "
                "or better — trade skipped (entry-price rule preserved)."
            )
            return False

        record.status = TradeStatus.FAILED
        record.reason = result.error
        self.db.insert_copied_trade(record)
        pending.status = OrderStatus.FAILED
        self._track_order(record, pending)
        self._history_dirty = True
        self.log.error(
            f"Order failed for '{target.market_question}': {result.error}"
        )
        return False

    def _adopt(self, record: CopiedTrade) -> None:
        """Bring a freshly opened position into the live registry."""
        if record.db_id is None:
            return
        self._positions[record.db_id] = record
        self.prices.track([record.token_id])
        if self.user_stream is not None:
            self.user_stream.track_markets(
                {t.market_id for t in self._positions.values() if t.market_id}
            )
        self._history_dirty = True
        self._dirty.set()

    def _track_order(self, trade: Optional[CopiedTrade],
                     order: OrderRecord) -> None:
        if trade is not None and order.trade_db_id is None:
            order.trade_db_id = trade.db_id
        if order.db_id is None:
            self.db.insert_order(order)
        else:
            self.db.update_order(order)
        if order.order_id:
            self._orders[order.order_id] = order
        self._orders_dirty = True
        self._dirty.set()

    # -- mirroring the target's exits ---------------------------------------

    async def _handle_target_exit(self, target_sell: TargetTrade) -> None:
        """The target sold; close any open copy of the same token to match."""
        open_copies = [t for t in self._positions.values()
                       if t.token_id == target_sell.token_id]
        if not open_copies:
            return
        for trade in open_copies:
            exit_price, _ = self._mark(trade)
            if exit_price <= 0:
                exit_price = await self._best_price(trade.token_id, Side.SELL) or \
                    trade.current_price or trade.my_price
            await self._close_position(trade, exit_price, target_sell)

    async def _close_position(self, trade: CopiedTrade, exit_price: float,
                              target_sell: Optional[TargetTrade] = None,
                              note: str = "") -> None:
        """Exit an open copied position at ``exit_price``."""
        tnote = (f" (target exited @ {target_sell.price:.3f})"
                 if target_sell else "")

        if self.settings.paper_trading or self.settings.dry_run:
            trade.current_price = exit_price
            trade.realized_pnl = (exit_price - trade.my_price) * trade.my_size
            if self.settings.paper_trading:
                self._paper_cash += exit_price * trade.my_size
            trade.status = TradeStatus.CLOSED
            trade.closed_ts = int(time.time())
            trade.reason = note or trade.reason
            self.db.update_copied_trade(trade)
            self._retire(trade)
            tag = "PAPER" if self.settings.paper_trading else "DRY-RUN"
            self.log.success(
                f"[{tag}] Exited copy — SOLD {trade.my_size:.2f} @ "
                f"{exit_price:.3f}{tnote}. Realized P/L "
                f"${trade.realized_pnl:,.2f}."
                + (f" Virtual cash: ${self._paper_cash:,.2f}."
                   if self.settings.paper_trading else "")
            )
            return

        # Live: place a Fill-And-Kill SELL at the current bid to exit now.
        order = OrderRecord(
            order_id="", token_id=trade.token_id, market_id=trade.market_id,
            market_question=trade.market_question, outcome=trade.outcome,
            side=Side.SELL, price=round(exit_price, 3), size=trade.my_size,
            status=OrderStatus.PENDING, trade_db_id=trade.db_id,
        )
        self.db.insert_order(order)
        self._orders_dirty = True

        result = await self.client.place_limit_order(
            token_id=trade.token_id, side=Side.SELL, price=exit_price,
            size=trade.my_size, order_type="FAK",
        )
        order.order_id = result.order_id or ""
        order.filled_size = result.filled_size
        order.error = result.error

        if result.success:
            fill = result.avg_price or exit_price
            trade.current_price = fill
            trade.realized_pnl = (fill - trade.my_price) * trade.my_size
            trade.status = TradeStatus.CLOSED
            trade.closed_ts = int(time.time())
            trade.reason = note or trade.reason
            self.db.update_copied_trade(trade)
            self._retire(trade)
            order.status = OrderStatus.FILLED
            self._track_order(trade, order)
            self.log.success(
                f"Exited copy{' (target sold)' if target_sell else ''} — SOLD "
                f"{trade.my_size:.2f} @ {fill:.3f}{tnote}. Realized P/L "
                f"${trade.realized_pnl:,.2f}."
            )
        else:
            order.status = OrderStatus.FAILED
            self._track_order(trade, order)
            self.log.error(
                f"Failed to exit copy for "
                f"'{trade.market_question or trade.token_id}': {result.error}"
            )

    def _record_skip(self, target: TargetTrade, reason: str) -> None:
        record = CopiedTrade(
            target_trade_id=target.trade_id,
            market_id=target.market_id,
            token_id=target.token_id,
            outcome=target.outcome,
            market_question=target.market_question,
            side=Side.BUY,
            mode=self.settings.trading_mode,
            target_price=target.price,
            target_usdc=target.usdc_size,
            market_end_ts=target.market_end_ts,
            opened_ts=int(time.time()),
            status=TradeStatus.SKIPPED,
            reason=reason,
        )
        self.db.insert_copied_trade(record)
        self._history_dirty = True
        self._dirty.set()

    # ==================================================================
    # exchange reconciliation  (keeps active trades in sync)
    # ==================================================================

    async def _reconcile(self) -> None:
        """Verify our local book matches what the exchange reports.

        Paper mode has no exchange state to compare against, so this is a no-op
        there. For live/dry-run accounts we check two things:

          * Position sizes — a partial fill, a manual trade in the Polymarket
            UI, or a redemption would otherwise leave our numbers drifting.
          * Resting orders — anything the exchange no longer lists is marked
            terminal so the Pending Orders panel can't show a ghost.
        """
        if self.settings.paper_trading or self.client is None:
            return

        wallet = self.client.funder_address
        if wallet:
            live = await self.data.get_positions(wallet)
            for trade in list(self._positions.values()):
                actual = live.get(trade.token_id)
                if actual is None:
                    # Nothing held. Only act once the market is genuinely done;
                    # the Data API can lag a fresh fill by a few seconds.
                    info = self.gamma.cached(trade.market_id)
                    if info is not None and (info.resolved or info.closed):
                        await self._settle_trade(trade, info)
                    elif time.time() - trade.opened_ts > 120:
                        trade.sync_note = "Not found on-chain — verify manually."
                    continue
                size = actual.get("size") or 0.0
                if abs(size - trade.my_size) > max(0.01, trade.my_size * 0.005):
                    self.log.warning(
                        f"Position size resynced for "
                        f"'{trade.market_question or trade.token_id}': "
                        f"{trade.my_size:.2f} → {size:.2f} shares (exchange)."
                    )
                    trade.my_size = size
                    trade.my_usdc = size * trade.my_price
                    trade.sync_note = "Size resynced from exchange."
                    self.db.update_copied_trade(trade)
                    self._dirty.set()
                else:
                    trade.sync_note = ""

        # Reconcile resting orders.
        pending = [o for o in self._orders.values() if o.is_pending]
        if not pending:
            return
        live_orders = await self.client.open_orders()
        live_ids = {str(o.get("id") or o.get("orderID") or "")
                    for o in live_orders}
        for order in pending:
            if order.order_id and order.order_id not in live_ids:
                order.status = (OrderStatus.FILLED if order.filled_size > 0
                                else OrderStatus.CANCELED)
                self.db.update_order(order)
                self._orders.pop(order.order_id, None)
                self._orders_dirty = True

    async def _on_user_event(self, kind: str, event: dict) -> None:
        """Live order/trade updates from the authenticated CLOB channel."""
        order_id = str(event.get("id") or event.get("order_id")
                       or event.get("orderID") or "")
        if not order_id:
            return
        order = self._orders.get(order_id)
        if order is None:
            return

        if kind == "order":
            try:
                order.filled_size = float(
                    event.get("size_matched") or event.get("sizeMatched") or
                    order.filled_size
                )
            except (TypeError, ValueError):
                pass
            status = str(event.get("type") or event.get("status") or "").upper()
            if status in ("CANCELLATION", "CANCELED", "CANCELLED"):
                order.status = OrderStatus.CANCELED
            elif order.filled_size >= order.size - 1e-9:
                order.status = OrderStatus.FILLED
            elif order.filled_size > 0:
                order.status = OrderStatus.MATCHED
            else:
                order.status = OrderStatus.LIVE
        elif kind == "trade":
            status = str(event.get("status") or "").upper()
            if status in ("CONFIRMED", "MINED", "MATCHED"):
                try:
                    order.filled_size = max(
                        order.filled_size, float(event.get("size") or 0)
                    )
                except (TypeError, ValueError):
                    pass
                order.status = (OrderStatus.FILLED
                               if order.filled_size >= order.size - 1e-9
                               else OrderStatus.MATCHED)
            elif status in ("FAILED", "RETRYING"):
                order.status = OrderStatus.FAILED

        self.db.update_order(order)
        if not order.is_pending:
            self._orders.pop(order_id, None)
        self._orders_dirty = True
        self._dirty.set()

    # ==================================================================
    # manual actions from the UI
    # ==================================================================

    async def close_position(self, trade_db_id: int) -> dict:
        trade = self._positions.get(trade_db_id)
        if trade is None:
            return {"ok": False, "error": "Position not found or already closed."}
        exit_price, _ = self._mark(trade)
        if exit_price <= 0:
            exit_price = await self._best_price(trade.token_id, Side.SELL) or 0.0
        if exit_price <= 0:
            return {"ok": False, "error": "No live bid to exit against."}
        async with self._copy_lock:
            await self._close_position(trade, exit_price,
                                       note="Closed manually from the dashboard.")
        return {"ok": True}

    async def cancel_order(self, order_id: str) -> dict:
        order = self._orders.get(order_id)
        if order is None:
            return {"ok": False, "error": "Order not found or already terminal."}
        if self.settings.paper_trading or self.settings.dry_run:
            order.status = OrderStatus.CANCELED
            self.db.update_order(order)
            self._orders.pop(order_id, None)
            self._orders_dirty = True
            return {"ok": True}
        ok = await self.client.cancel_order(order_id)
        if ok:
            order.status = OrderStatus.CANCELED
            self.db.update_order(order)
            self._orders.pop(order_id, None)
            self._orders_dirty = True
            self.log.info(f"Order {order_id} cancelled.")
        return {"ok": ok, "error": "" if ok else "Exchange refused the cancel."}

    # ==================================================================
    # payloads
    # ==================================================================

    def _positions_payload(self) -> list[dict]:
        return [t.to_dict() for t in sorted(
            self._positions.values(),
            key=lambda t: (t.market_end_ts or 1 << 62, t.opened_ts),
        )]

    def _history_payload(self) -> list[dict]:
        return [t.to_dict() for t in self.db.get_closed_trades(limit=500)]

    def _orders_payload(self) -> list[dict]:
        return [o.to_dict() for o in self.db.get_orders(limit=300)]

    def _stats_payload(self) -> dict:
        stats = self.db.daily_stats(_day_start_ts())
        unrealized = sum(t.unrealized_pnl for t in self._positions.values())
        cost = sum(t.my_usdc for t in self._positions.values())
        value = sum(t.market_value for t in self._positions.values())
        stats.update({
            "openPositions": len(self._positions),
            "unrealizedPnl": unrealized,
            "openCost": cost,
            "openValue": value,
            "openReturnPct": (unrealized / cost * 100.0) if cost else 0.0,
            "portfolioPnl": unrealized + stats["realizedPnlToday"],
            "targetAccountValue": self._target_value,
        })
        return stats

    def _feeds_payload(self) -> dict:
        return {
            "prices": self.prices.health(),
            "user": self.user_stream.health() if self.user_stream else
            {"state": "off", "error": "", "markets": 0, "reconnects": 0},
            "books": self.prices.snapshot(),
        }

    def status(self) -> dict:
        mode = ("Fixed 50%" if self.settings.trading_mode == MODE_FIXED_50
                else "Proportional")
        return {
            "state": self.state,
            "detail": self.detail,
            "connected": self.connected,
            "running": self.state == "running",
            "mode": self.settings.trading_mode,
            "modeLabel": mode,
            "paper": self.settings.paper_trading,
            "dryRun": self.settings.dry_run,
            "targetWallet": self.settings.target_wallet,
            "address": (self._snapshot.connected_address
                        or (self.client.address if self.client else "")),
            "startedTs": self.started_ts,
            "uptime": (time.time() - self.started_ts) if self.started_ts else 0,
        }

    def _publish_state(self) -> None:
        self.hub.publish("state", self.status())
