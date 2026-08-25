"""Execution simulation. No idealised fills.

The difference between a copy backtest and a fiction is one rule:

    if no price printed inside the fill window, the trade is UNFILLED

Not "filled at the signal price". Not "filled at the wallet's price". Unfilled.
V2 established this and it stays, because relaxing it is the single change that
would most improve every reported number while making all of them false.

Every fill records six quantities so that slippage is measured rather than
assumed:

    SIGNAL_PRICE    the price that triggered the decision
    EXPECTED_FILL   what our model said we would pay
    ACTUAL_FILL     what the tape says we would have paid
    SLIPPAGE        actual - signal
    LATENCY         signal to order, modelled
    MARKET_IMPACT   our own size moving the price against us

`uncertainty` lists what could not be modelled on this particular fill. On the
current data that list is never empty — there is no historical order book — and
a fill that carries uncertainty is not permitted into LIVE mode by
EXECUTION_VALIDITY.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum

from ..config import Settings
from ..core.source import HistoricalSource


class FillStatus(str, Enum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    UNFILLED = "UNFILLED"
    REJECTED = "REJECTED"


@dataclass
class SimulatedFill:
    status: FillStatus
    fill_id: str = ""
    signal_price: float = 0.0
    expected_fill: float = 0.0
    actual_fill: float = 0.0
    slippage: float = 0.0
    latency_ms: int = 0
    market_impact: float = 0.0
    size_usdc: float = 0.0
    size_shares: float = 0.0
    requested_usdc: float = 0.0
    fees: float = 0.0
    fill_ts: int = 0
    uncertainty: list = field(default_factory=list)
    reason: str = ""

    @property
    def fill_ratio(self) -> float:
        return self.size_usdc / self.requested_usdc if self.requested_usdc else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["fill_ratio"] = round(self.fill_ratio, 4)
        return d


class ExecutionSimulator:
    def __init__(self, st: Settings, source: HistoricalSource | None = None) -> None:
        self.st = st
        self.source = source or HistoricalSource(st)

    def simulate(self, *, token_id: str, signal_ts: int, signal_price: float,
                 size_usdc: float, book_layer=None,
                 fill_window_secs: int = 900) -> SimulatedFill:
        """Model one entry.

        Prefers a measured book when one exists; falls back to the tape. The
        two paths report different `uncertainty`, so a reader can always tell
        which happened — a simulator that silently degrades from measured to
        modelled is one that reports modelled results as measured ones.
        """
        f = SimulatedFill(status=FillStatus.UNFILLED,
                          fill_id=uuid.uuid4().hex[:16],
                          signal_price=signal_price,
                          requested_usdc=round(size_usdc, 6),
                          latency_ms=self.st.costs.latency_ms)

        if size_usdc <= 0:
            f.status = FillStatus.REJECTED
            f.reason = "zero size"
            return f

        # Our order cannot be placed before our own latency has elapsed.
        earliest = signal_ts + math.ceil(self.st.costs.latency_ms / 1000.0)

        if book_layer is not None and book_layer.ok:
            return self._from_book(f, book_layer, earliest)
        return self._from_tape(f, token_id, earliest, fill_window_secs)

    # -- book path ----------------------------------------------------------
    def _from_book(self, f: SimulatedFill, book, earliest: int) -> SimulatedFill:
        levels = book.get("levels") or []
        asks = [(float(p), float(s)) for p, s, *_ in
                (l for l in levels if len(l) >= 2)] if levels else []
        asks = sorted([a for a in asks if a[0] > 0], key=lambda x: x[0])
        if not asks:
            best_ask = book.get("best_ask")
            depth = book.get("ask_depth")
            if not best_ask or not depth:
                f.reason = "book present but carries no usable ask side"
                f.uncertainty = ["book levels empty"]
                return f
            asks = [(float(best_ask), float(depth))]

        # Walk the book. This is where market impact becomes a measurement
        # rather than a constant: a $5 order at $100 equity will usually clear
        # at the top level, and the simulator should say so rather than charge
        # a flat impact penalty.
        remaining = f.requested_usdc
        spent = shares = 0.0
        for px, sz in asks:
            avail = px * sz * self.st.costs.fill_ratio_assumption
            take = min(remaining, avail)
            if take <= 0:
                continue
            shares += take / px
            spent += take
            remaining -= take
            if remaining <= 1e-9:
                break

        if spent <= 0:
            f.reason = "no takeable size on the ask side"
            return f
        f.actual_fill = round(spent / shares, 6)
        f.expected_fill = round(asks[0][0] * (
            1 + self.st.costs.slippage_bps / 10_000.0), 6)
        f.size_usdc = round(spent, 6)
        f.size_shares = round(shares, 6)
        f.slippage = round(f.actual_fill - f.signal_price, 6)
        f.market_impact = round(f.actual_fill - asks[0][0], 6)
        f.fees = round(spent * self.st.costs.fee_bps / 10_000.0, 6)
        f.fill_ts = max(earliest, book.as_of)
        f.status = (FillStatus.FILLED if remaining <= 1e-6
                    else FillStatus.PARTIAL)
        f.uncertainty = ["queue position unknown",
                         "book may move within latency window"]
        if f.status is FillStatus.PARTIAL:
            f.reason = (f"book supplied ${spent:.2f} of ${f.requested_usdc:.2f} "
                        f"at the assumed "
                        f"{self.st.costs.fill_ratio_assumption:.0%} take ratio")
        return f

    # -- tape path ----------------------------------------------------------
    def _from_tape(self, f: SimulatedFill, token_id: str, earliest: int,
                   window: int) -> SimulatedFill:
        if not self.source.available:
            f.reason = "no tape source"
            f.uncertainty = ["no data"]
            return f
        prints = self.source.prints(token_id, earliest + window,
                                    lookback_secs=window + 60, limit=400)
        future = [(t, p, n) for t, p, n, _ in prints if t >= earliest]
        if not future:
            f.reason = (f"no price printed within {window}s of the signal; the "
                        f"copy could not have been executed at a knowable price")
            f.uncertainty = ["no print in fill window"]
            return f

        ts, px, notional = future[0]
        # We pay the first print at or after our order arrives, plus configured
        # slippage. Never the signal price — that price is already gone.
        f.actual_fill = round(min(0.999, px * (
            1 + self.st.costs.slippage_bps / 10_000.0)), 6)
        f.expected_fill = round(min(0.999, f.signal_price * (
            1 + self.st.costs.slippage_bps / 10_000.0)), 6)
        f.fill_ts = ts
        f.slippage = round(f.actual_fill - f.signal_price, 6)

        # Impact proxy: our order relative to the size that actually printed.
        # Crude, and labelled as such in `uncertainty`.
        ratio = f.requested_usdc / max(notional, 1.0)
        f.market_impact = round(min(0.05, 0.01 * math.log1p(ratio)), 6)
        f.actual_fill = round(min(0.999, f.actual_fill + f.market_impact), 6)

        takeable = notional * self.st.costs.fill_ratio_assumption
        filled = min(f.requested_usdc, takeable)
        if filled < self.st.capital.min_order_usdc:
            f.reason = (f"only ${takeable:.2f} printed at this level; below the "
                        f"${self.st.capital.min_order_usdc:.2f} minimum order")
            return f
        f.size_usdc = round(filled, 6)
        f.size_shares = round(filled / f.actual_fill, 6)
        f.fees = round(filled * self.st.costs.fee_bps / 10_000.0, 6)
        f.status = (FillStatus.FILLED if filled >= f.requested_usdc - 1e-6
                    else FillStatus.PARTIAL)
        f.uncertainty = ["depth unmeasured", "spread unmeasured",
                         "queue position unknown",
                         "impact is a tape-size proxy, not a book walk"]
        if f.status is FillStatus.PARTIAL:
            f.reason = (f"tape supplied ${filled:.2f} of ${f.requested_usdc:.2f}")
        return f


# --------------------------------------------------------------------------
# Segmented / staged execution
# --------------------------------------------------------------------------

@dataclass
class Slice:
    offset_secs: int
    usdc: float
    rationale: str


def plan_segments(*, total_usdc: float, liquidity_per_hour: float,
                  urgency: float, st: Settings) -> list:
    """Split an order into slices, or decline to split it.

    Never copies a large wallet's notional. What is copied is the *exposure
    logic*: if a wallet takes 3% of its book, we take 3% of ours, and then ask
    separately whether our 3% can be filled here at all.

    At $100 of equity most orders are one slice, because a $5 order split three
    ways is three orders below the venue minimum. That is the correct answer
    and the function returns it rather than producing a plan that cannot trade.
    """
    if total_usdc <= 0:
        return []
    min_slice = st.capital.min_order_usdc
    max_slices = max(1, int(total_usdc // max(min_slice, 0.01)))
    if max_slices <= 1:
        return [Slice(0, round(total_usdc, 4),
                      f"single slice: ${total_usdc:.2f} cannot be split above "
                      f"the ${min_slice:.2f} venue minimum")]

    # How much of an hour's liquidity we would consume in one go.
    participation = total_usdc / max(liquidity_per_hour, 1e-9)
    if participation < 0.02:
        return [Slice(0, round(total_usdc, 4),
                      f"single slice: order is {participation:.1%} of hourly "
                      f"flow, below the 2% impact threshold")]

    # More slices when we are a large share of flow; fewer when urgent, because
    # a staged entry into a moving market is a slower way to pay more.
    want = min(max_slices, max(2, int(math.ceil(participation / 0.02))))
    want = max(2, int(round(want * (1.0 - 0.5 * max(0.0, min(1.0, urgency))))))
    per = total_usdc / want
    if per < min_slice:
        want = max(1, int(total_usdc // min_slice))
        per = total_usdc / want
    spacing = int(max(30, 3600 * 0.02 / max(participation / want, 1e-9)))
    spacing = min(spacing, 1800)
    return [Slice(i * spacing, round(per, 4),
                  f"slice {i + 1}/{want}: {participation / want:.1%} of hourly "
                  f"flow each, {spacing}s apart")
            for i in range(want)]
