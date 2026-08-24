"""
bridge -> Polymarket, outbound (prompt section 6).

The second and last file that knows Polymarket exists. It takes a
:class:`Decision`, makes it a legal order, sends it, and reports what actually
happened — fills, partial fills, rejections, real execution prices, fees — back
as an :class:`ExecutionReport` for the runner and the journal.

Three properties matter more than throughput here:

  * **No duplicate orders.** A token with an order in flight is refused a second
    one, and a retry only ever happens for a failure we can prove created
    nothing (a connection-level error with no order id). Anything ambiguous
    fails safely and is retried next cycle from fresh state, where the
    reconciler has had a chance to establish the truth.
  * **Dry-run is the same code path.** The simulator sits at the last possible
    moment — after sizing, after validation — so a dry-run session exercises
    everything except the network call. Orders that would be illegal are still
    rejected on paper.
  * **Every attempt is reported.** A rejection is as much an event as a fill;
    silence would leave the bridge believing an order is live.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..config import Config
from ..logs import Log
from ..models import (
    Action, Decision, ExecutionReport, MarketFeatures, PositionView,
)
from ..upstream import OrderResult, PolymarketError, Side, TradingClient
from .sizing import SizedOrder, size_buy, size_sell

# Failures that are safe to retry: the request did not reach the matching engine,
# so no order can exist. Anything outside this set is treated as ambiguous.
_TRANSIENT = (
    "timeout", "timed out", "connection", "connect", "temporarily",
    "502", "503", "504", "bad gateway", "gateway timeout", "read error",
    "remote end closed", "reset by peer",
)


def _is_transient(error: str) -> bool:
    lowered = (error or "").lower()
    return any(token in lowered for token in _TRANSIENT)


# --- simulated account ------------------------------------------------------

@dataclass
class PaperPosition:
    token_id: str
    market_id: str = ""
    outcome: str = ""
    question: str = ""
    size: float = 0.0
    avg_price: float = 0.0
    opened_ts: int = 0
    end_ts: Optional[int] = None
    lifecycle_id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "tokenId": self.token_id, "marketId": self.market_id,
            "outcome": self.outcome, "question": self.question,
            "size": self.size, "avgPrice": self.avg_price,
            "openedTs": self.opened_ts, "endTs": self.end_ts,
            "lifecycleId": self.lifecycle_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PaperPosition":
        return cls(
            token_id=str(data.get("tokenId") or ""),
            market_id=str(data.get("marketId") or ""),
            outcome=str(data.get("outcome") or ""),
            question=str(data.get("question") or ""),
            size=float(data.get("size") or 0.0),
            avg_price=float(data.get("avgPrice") or 0.0),
            opened_ts=int(data.get("openedTs") or 0),
            end_ts=data.get("endTs"),
            lifecycle_id=data.get("lifecycleId"),
        )


class PaperBook:
    """A simulated portfolio with the same cash mechanics as the real thing.

    Cost is deducted on entry and proceeds credited on exit, so the
    doubling rule sees a portfolio value that actually compounds — which is the
    only way a dry-run session can exercise it.
    """

    STATE_KEY = "paper.book"

    def __init__(self, store, starting_cash: float, reset: bool = False,
                 fee_per_trade: float = 0.0, fee_fund: float = 0.0):
        self.store = store
        self.starting_cash = float(starting_cash)
        # Charged on every fill, in and out. Small in absolute terms and large
        # relative to the first steps of the progression: a 1c fee on a $0.19
        # trade is 5.3% one way, so a simulation that ignored it would overstate
        # early results by more than the edge it is trying to measure.
        self.fee_per_trade = max(0.0, float(fee_per_trade))
        self.fees_paid = 0.0
        # The fee fund: money set aside purely for fees, per the operator's
        # rule "replenish the fee balance when it gets down to 10% of where it
        # started". Fees draw the fund down; at the 10% floor it refills to its
        # starting amount and the top-up is counted, so both the dashboard and
        # a live operator can see fee money never silently runs dry.
        self.fee_fund_start = max(0.0, float(fee_fund))
        self.fee_fund = self.fee_fund_start
        self.fee_fund_topups = 0
        saved = None if reset else store.get_state(self.STATE_KEY, None)
        if isinstance(saved, dict):
            self.cash = float(saved.get("cash", starting_cash))
            self.realized_pnl = float(saved.get("realizedPnl", 0.0))
            self.fees_paid = float(saved.get("feesPaid", 0.0))
            self.fee_fund = float(saved.get("feeFund", self.fee_fund_start))
            self.fee_fund_topups = int(saved.get("feeFundTopups", 0))
            self.positions = {
                str(k): PaperPosition.from_dict(v)
                for k, v in (saved.get("positions") or {}).items()
            }
        else:
            self.cash = float(starting_cash)
            self.realized_pnl = 0.0
            self.positions: dict[str, PaperPosition] = {}
            self._persist()

    def _charge_fee(self) -> None:
        self.fees_paid += self.fee_per_trade
        if self.fee_fund_start <= 0:
            return
        self.fee_fund = max(0.0, self.fee_fund - self.fee_per_trade)
        # The epsilon matters: repeated float subtraction leaves the fund at
        # 0.10000000000000014 instead of 0.10, and without it the refill fires
        # one fill late — the rule says AT 10%, not just under it.
        if self.fee_fund <= 0.10 * self.fee_fund_start + 1e-9:
            # The 10% rule: refill to the starting amount rather than letting
            # fee money run out mid-session.
            self.fee_fund = self.fee_fund_start
            self.fee_fund_topups += 1

    def _persist(self) -> None:
        self.store.set_state(self.STATE_KEY, {
            "cash": self.cash,
            "realizedPnl": self.realized_pnl,
            "feesPaid": self.fees_paid,
            "feeFund": self.fee_fund,
            "feeFundStart": self.fee_fund_start,
            "feeFundTopups": self.fee_fund_topups,
            "positions": {k: v.to_dict() for k, v in self.positions.items()},
        })

    def buy(self, decision: Decision, price: float, shares: float,
            end_ts: Optional[int] = None) -> PaperPosition:
        cost = shares * price + self.fee_per_trade
        self.cash = max(0.0, self.cash - cost)
        self._charge_fee()
        existing = self.positions.get(decision.token_id)
        if existing is None:
            existing = PaperPosition(
                token_id=decision.token_id, market_id=decision.market_id,
                outcome=decision.outcome, question=decision.question,
                size=shares, avg_price=price, opened_ts=int(time.time()),
                end_ts=end_ts,
            )
            self.positions[decision.token_id] = existing
        else:
            total = existing.size + shares
            existing.avg_price = (
                (existing.avg_price * existing.size + price * shares) / total
                if total else price
            )
            existing.size = total
        self._persist()
        return existing

    def sell(self, token_id: str, price: float, shares: float) -> float:
        """Credit proceeds and return the realized P&L on the shares sold."""
        position = self.positions.get(token_id)
        if position is None:
            return 0.0
        shares = min(shares, position.size)
        # The fee is part of the result, not an afterthought. A trade that gains
        # less than the round trip costs is a loss, and the journal has to say
        # so — otherwise the feedback loop learns from a number that never
        # happened and keeps repeating a "winner" that lost money.
        pnl = (price - position.avg_price) * shares - self.fee_per_trade
        self.cash = max(0.0, self.cash + price * shares - self.fee_per_trade)
        self._charge_fee()
        self.realized_pnl += pnl
        position.size = round(position.size - shares, 6)
        if position.size <= 1e-6:
            self.positions.pop(token_id, None)
        self._persist()
        return pnl

    def settle(self, token_id: str, final_price: float) -> float:
        """Resolve a position at its settlement price (1.0 or 0.0)."""
        position = self.positions.get(token_id)
        if position is None:
            return 0.0
        return self.sell(token_id, final_price, position.size)

    def views(self, mark_for) -> list[PositionView]:
        out: list[PositionView] = []
        for position in self.positions.values():
            mark = mark_for(position.token_id) or position.avg_price
            out.append(PositionView(
                token_id=position.token_id, market_id=position.market_id,
                outcome=position.outcome, question=position.question,
                size=position.size, avg_price=position.avg_price,
                cur_price=mark, opened_ts=position.opened_ts,
                end_ts=position.end_ts, lifecycle_id=position.lifecycle_id,
                source="simulated",
            ))
        return out

    def value(self, mark_for) -> float:
        return sum(p.size * (mark_for(p.token_id) or p.avg_price)
                   for p in self.positions.values())

    def link_lifecycle(self, token_id: str, lifecycle_id: int) -> None:
        position = self.positions.get(token_id)
        if position is not None:
            position.lifecycle_id = lifecycle_id
            self._persist()


# --- the adapter ------------------------------------------------------------

@dataclass
class _InFlight:
    token_id: str
    started: float = field(default_factory=time.time)


class PolymarketExecutionAdapter:
    def __init__(self, config: Config, log: Log, journal,
                 client: Optional[TradingClient] = None,
                 paper: Optional[PaperBook] = None,
                 price_fn=None):
        self.config = config
        self.log = log
        self.journal = journal
        self.client = client
        self.paper = paper
        # async (token_id, Side) -> Optional[float]: a confirmed touch, fetched
        # over REST when the streamed book has not arrived yet. Without it an
        # order can only be sized from whatever the last cycle happened to see.
        self.price_fn = price_fn
        self._in_flight: dict[str, _InFlight] = {}
        # client_order_id -> time submitted. An identical order re-emitted
        # within the idempotency window is suppressed rather than re-sent.
        self._recent_orders: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.submitted = 0
        self.rejected = 0
        self.simulated = 0
        self.deduplicated = 0

    @property
    def live(self) -> bool:
        return self.config.mode.live and self.client is not None

    @property
    def pending_count(self) -> int:
        return len(self._in_flight)

    # -- public entry point --------------------------------------------------

    async def execute(self, decision: Decision,
                      market: Optional[MarketFeatures],
                      position: Optional[PositionView],
                      available_cash: float,
                      min_trade_size: float) -> ExecutionReport:
        """Validate, size and send one decision. Never raises."""
        report = ExecutionReport(decision=decision, simulated=not self.live)
        if not decision.is_actionable:
            report.status = "NO_ACTION"
            return report

        # Claim the token before doing anything that awaits. Claiming after the
        # price fetch would leave that round trip unguarded, and two concurrent
        # executions could both pass the check and both send an order — which
        # is precisely the duplicate this guard exists to prevent.
        async with self._lock:
            if decision.token_id in self._in_flight:
                report.status = "SKIPPED_IN_FLIGHT"
                report.error = "An order for this token is already in flight."
                self.log.warning("Refusing a second order for a token",
                                 token=decision.token_id[:12])
                return report
            self._in_flight[decision.token_id] = _InFlight(decision.token_id)

        try:
            touch = await self._touch(decision, market)
            sized = self._size(decision, market, position, available_cash,
                               min_trade_size, touch)
            if not sized.ok:
                report.status = "INVALID"
                report.error = sized.reason
                self.rejected += 1
                self.log.event("execution.invalid",
                               action=decision.action.value,
                               token=decision.token_id[:12],
                               reason=sized.reason)
                return report

            decision.limit_price = sized.price
            decision.size_shares = sized.shares
            decision.size_usdc = sized.usdc
            report.requested_size = sized.shares

            # Hard per-order cap (risk §3). A BUY over the cap is rejected, not
            # trimmed — a size that large means something upstream is wrong, and
            # quietly shrinking it would hide that. Exits are never capped:
            # getting out of a position is how you reduce risk.
            cap = self.config.risk.max_single_order_usdc
            if cap > 0 and decision.action is Action.BUY and sized.usdc > cap:
                report.status = "REJECTED_RISK"
                report.error = (f"Order ${sized.usdc:,.2f} exceeds "
                                f"max_single_order_usdc ${cap:,.2f}.")
                self.rejected += 1
                self.log.event("execution.risk_block", reason="single_order",
                               token=decision.token_id[:12],
                               usdc=round(sized.usdc, 2), cap=cap)
                return report

            coid = self._client_order_id(decision, sized)
            report.client_order_id = coid

            # Idempotency: an identical order (same token/side/price/size) seen
            # again inside the window is suppressed. This is the guard that
            # stops an ambiguous-retry or a repeated cycle from doubling a
            # position — the in-flight lock only covers the same cycle.
            if self.live and self._is_duplicate(coid):
                report.status = "SKIPPED_DUPLICATE"
                report.error = "Idempotent: identical order recently submitted."
                self.deduplicated += 1
                self.log.warning("Idempotent skip of a duplicate order",
                                 token=decision.token_id[:12], coid=coid)
                return report

            if self.live:
                await self._send_live(decision, sized, report)
                if report.submitted:
                    self._record_order(coid)
            else:
                self._simulate(decision, market, sized, report)
        except Exception as exc:                     # pragma: no cover - guard
            report.status = "ERROR"
            report.error = f"{type(exc).__name__}: {exc}"
            self.log.error("Execution raised", token=decision.token_id[:12],
                           error=report.error)
        finally:
            self._in_flight.pop(decision.token_id, None)
        return report

    # -- idempotency ---------------------------------------------------------

    def _client_order_id(self, decision: Decision, sized: SizedOrder) -> str:
        """A deterministic id for this exact order.

        Two orders are "the same" when they would put the same size into the
        same token on the same side at the same price. Derived from those facts
        so a resend produces the *same* id and is recognised as a duplicate —
        which is what makes the order idempotent at our layer, independent of
        the salt py-clob-client puts in the signature.
        """
        raw = (f"{decision.token_id}|{self._side_of(decision.action).value}|"
               f"{round(sized.price, 3)}|{round(sized.shares, 2)}")
        return "pqb-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def _is_duplicate(self, coid: str) -> bool:
        ttl = max(0, self.config.risk.idempotency_ttl_seconds)
        if ttl == 0:
            return False
        cutoff = time.time() - ttl
        # Prune while we are here so the ledger cannot grow without bound.
        self._recent_orders = {k: v for k, v in self._recent_orders.items()
                               if v >= cutoff}
        return coid in self._recent_orders

    def _record_order(self, coid: str) -> None:
        self._recent_orders[coid] = time.time()

    # -- sizing --------------------------------------------------------------

    def _side_of(self, action: Action) -> Side:
        return Side.BUY if action is Action.BUY else Side.SELL

    async def _touch(self, decision: Decision,
                     market: Optional[MarketFeatures]) -> Optional[float]:
        """The executable price for this decision: the ask to buy, bid to sell.

        Taken from the streamed book when it has one, and otherwise fetched
        over REST — a ~100ms round trip is a small price for not guessing what
        an order will execute at. Returns None when no touch can be
        established at all, which the caller must treat as "do not send".

        This exists because the book is *not* always populated when a decision
        needs executing: the first cycle after a restart, a market that has
        just been pinned, or a token the stream has not delivered yet. Sizing
        an exit from the position's own entry price in those moments produces
        a sell limit above the market that a Fill-And-Kill order simply kills —
        no exit, and a fabricated P&L of exactly zero in the journal.
        """
        side = self._side_of(decision.action)
        quote = market.quote(decision.token_id) if market else None
        if quote is not None:
            price = quote.ask if side is Side.BUY else quote.bid
            if price is not None and price > 0:
                return price
        if self.price_fn is None:
            return None
        try:
            price = await self.price_fn(decision.token_id, side)
        except Exception as exc:
            self.log.warning("Could not fetch a touch price",
                             token=decision.token_id[:12], error=repr(exc))
            return None
        if price is not None and price > 0:
            self.log.event("execution.touch_refetched",
                           token=decision.token_id[:12],
                           side=side.value, price=price)
            return price
        return None

    def _size(self, decision: Decision, market: Optional[MarketFeatures],
              position: Optional[PositionView], available_cash: float,
              min_trade_size: float,
              touch: Optional[float]) -> SizedOrder:
        quote = market.quote(decision.token_id) if market else None
        tick = quote.tick_size if quote else (market.tick_size if market else 0.01)
        market_min = market.min_order_size if market else 0.0
        portfolio = self.config.engine.portfolio

        if decision.action is Action.BUY:
            if market is not None and not market.tradable:
                return SizedOrder(False, reason="Market is not tradable.")
            if touch is None:
                # The midpoint would be a guess, and a guess on the entry price
                # is exactly what the entry rules exist to prevent.
                return SizedOrder(False, reason="No ask on the book.")
            from .sizing import effective_min_usdc
            return size_buy(
                desired_usdc=decision.size_usdc,
                ask=touch,
                tick=tick,
                available_cash=available_cash,
                min_usdc=effective_min_usdc(portfolio.min_order_usdc,
                                            min_trade_size),
                market_min_shares=market_min,
                reserve_fraction=portfolio.reserve_cash_fraction,
                max_price=self.config.engine.entry.max_price,
            )

        if position is None or position.size <= 0:
            return SizedOrder(False, reason="No position to sell.")
        if touch is None:
            # Deliberately NOT falling back to the entry price or the last
            # mark. Both are prices we wish existed rather than prices anyone
            # is bidding; an order at either would not fill, while the journal
            # would record a clean exit that never happened.
            return SizedOrder(
                False,
                reason="No live bid — refusing to price an exit from the "
                       "entry price. Will retry next cycle.")
        fraction = (self.config.engine.exits.reduce_fraction
                    if decision.action is Action.REDUCE else 1.0)
        if decision.action is Action.SELL and decision.size_shares > 0:
            fraction = min(1.0, decision.size_shares / position.size)
        return size_sell(shares_held=position.size, bid=touch, tick=tick,
                         fraction=fraction, market_min_shares=market_min)

    # -- dry-run -------------------------------------------------------------

    def _simulate(self, decision: Decision, market: Optional[MarketFeatures],
                  sized: SizedOrder, report: ExecutionReport) -> None:
        """Fill on paper at the quoted price, capped by visible depth.

        Filling the full requested size regardless of the book would make a
        dry-run flatter than reality on exactly the markets where it matters
        most — the thin ones. Where depth is known, it is respected.
        """
        quote = market.quote(decision.token_id) if market else None
        fillable = sized.shares
        if quote is not None:
            depth = (quote.ask_depth if decision.action is Action.BUY
                     else quote.bid_depth)
            if depth > 0:
                fillable = min(fillable, depth)
        fillable = round(fillable, 2)

        if fillable <= 0:
            report.status = "SIMULATED_NO_FILL"
            report.error = "No depth at the quoted price."
            self.simulated += 1
            return

        report.submitted = True
        report.simulated = True
        report.order_id = f"SIM-{int(time.time() * 1000)}"
        report.filled_size = fillable
        report.avg_price = sized.price
        report.status = ("SIMULATED_FILLED" if fillable >= sized.shares - 1e-9
                         else "SIMULATED_PARTIAL")
        report.fee = 0.0
        self.simulated += 1

        if self.paper is not None:
            if decision.action is Action.BUY:
                self.paper.buy(decision, sized.price, fillable,
                               end_ts=market.end_ts if market else None)
            else:
                self.paper.sell(decision.token_id, sized.price, fillable)

        self.log.event("execution.simulated", action=decision.action.value,
                       token=decision.token_id[:12], price=sized.price,
                       shares=fillable, usdc=round(fillable * sized.price, 4),
                       status=report.status)

    # -- live ----------------------------------------------------------------

    async def _send_live(self, decision: Decision, sized: SizedOrder,
                         report: ExecutionReport) -> None:
        side = self._side_of(decision.action)
        attempts = 0
        delay = 1.0
        while attempts < 3:
            attempts += 1
            try:
                result: OrderResult = await self.client.place_limit_order(
                    token_id=decision.token_id, side=side, price=sized.price,
                    size=sized.shares, order_type="FAK",
                )
            except PolymarketError as exc:
                result = OrderResult(success=False, error=str(exc))
            except Exception as exc:
                result = OrderResult(success=False,
                                     error=f"{type(exc).__name__}: {exc}")

            if result.success:
                self._apply_live_result(decision, sized, report, result)
                return

            # Only a provably-nothing-happened failure is retried. An order id
            # means the exchange saw it; an unrecognised error means we cannot
            # tell, and re-sending could double the position.
            if result.order_id or not _is_transient(result.error):
                report.submitted = True
                report.status = "REJECTED"
                report.order_id = result.order_id
                report.error = result.error
                self.rejected += 1
                self.log.error("Order rejected", token=decision.token_id[:12],
                               attempt=attempts, error=result.error)
                return

            self.log.warning("Transient order failure — retrying",
                             token=decision.token_id[:12], attempt=attempts,
                             error=result.error, delay=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8.0)

        report.status = "FAILED_RETRIES"
        report.error = "Exhausted retries on transient errors."
        self.rejected += 1
        self.log.error("Order abandoned after retries",
                       token=decision.token_id[:12])

    def _apply_live_result(self, decision: Decision, sized: SizedOrder,
                           report: ExecutionReport,
                           result: OrderResult) -> None:
        report.submitted = True
        report.simulated = False
        report.order_id = result.order_id
        report.filled_size = result.filled_size
        report.avg_price = result.avg_price or sized.price
        report.fee = 0.0        # Polymarket publishes no per-fill fee today
        if result.filled_size <= 0:
            report.status = "NO_FILL"
        elif result.filled_size >= sized.shares - 1e-9:
            report.status = "FILLED"
        else:
            report.status = "PARTIAL"
        self.submitted += 1
        self.log.event("execution.live", action=decision.action.value,
                       token=decision.token_id[:12], price=report.avg_price,
                       requested=sized.shares, filled=report.filled_size,
                       order=result.order_id, status=report.status)

    # -- kill switch / mass actions ------------------------------------------

    async def cancel_all(self) -> bool:
        """Cancel every resting order. Used by the kill switch and the
        doubling rule's flatten phase."""
        if not self.live:
            self.log.event("execution.cancel_all", simulated=True)
            return True
        try:
            ok = await self.client.cancel_all()
        except Exception as exc:
            self.log.error("cancel_all failed", error=repr(exc))
            return False
        self.log.event("execution.cancel_all", ok=ok)
        return bool(ok)

    def stats(self) -> dict[str, Any]:
        return {"submitted": self.submitted, "rejected": self.rejected,
                "simulated": self.simulated, "inFlight": len(self._in_flight)}
