"""Realistic execution modelling, and explicit uncertainty where it cannot be.

Rule: do not assume every historical wallet trade could be copied at exactly
the same price. Where exact historical execution cannot be reconstructed, mark
the uncertainty rather than picking a number that looks reasonable.

What this models, with the data that exists:
  * the delayed tape price -- a real print, or UNFILLED
  * slippage and fees
  * a per-order notional cap
  * price drift out of the strategy's own band between signal and fill

What it CANNOT model on the historical substrate, and says so instead of
guessing:
  * order-book depth        no historical book exists in this database
  * partial fills           requires depth
  * queue position/latency  requires a book and timestamps finer than 1s
  * market impact of the copy itself

`DepthPolicy` exists so the live path can apply a real depth check the moment
book snapshots are captured, while the backtest path reports UNKNOWN rather
than silently passing. A depth gate that always passes in the backtest and
always fires in production is worse than no gate: it makes the backtest a
different strategy from the one that trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..config import Settings


class DepthState(str, Enum):
    OK = "OK"
    INSUFFICIENT = "INSUFFICIENT"
    UNKNOWN = "UNKNOWN"       # no book data -- never treated as OK


@dataclass
class ExecutionResult:
    filled: bool
    price: float = 0.0
    stake: float = 0.0
    gate_key: str = ""
    reason: str = ""
    depth_state: str = DepthState.UNKNOWN.value
    slippage_paid: float = 0.0
    uncertainty: list = None

    def to_dict(self) -> dict:
        return {"filled": self.filled, "price": round(self.price, 5),
                "stake": round(self.stake, 2), "gate_key": self.gate_key,
                "reason": self.reason, "depth_state": self.depth_state,
                "slippage_paid": round(self.slippage_paid, 5),
                "uncertainty": self.uncertainty or []}


class DepthPolicy:
    """Depth checking, honest about whether it has data.

    `min_multiple` is the same idea as the V1 `min_depth_x_stake` rule, which
    the audit reclassified from STRATEGY_A to EXECUTION: whether a fill is
    achievable is true regardless of which strategy asked for it.
    """

    def __init__(self, min_multiple: float = 3.0) -> None:
        self.min_multiple = min_multiple

    def check(self, stake: float, depth: float | None) -> tuple:
        if depth is None:
            return DepthState.UNKNOWN, (
                "no order-book snapshot for this market; depth cannot be "
                "checked. This is a DATA gap, not a pass.")
        if depth <= 0:
            return DepthState.INSUFFICIENT, "book is empty"
        if depth < stake * self.min_multiple:
            return DepthState.INSUFFICIENT, (
                f"depth ${depth:,.0f} is under {self.min_multiple:.0f}x the "
                f"${stake:,.0f} stake - the fill would eat the edge")
        return DepthState.OK, ""


class ExecutionModel:
    def __init__(self, st: Settings, tape=None,
                 depth_policy: DepthPolicy | None = None,
                 require_depth: bool = False) -> None:
        self.st = st
        self.tape = tape
        self.depth = depth_policy or DepthPolicy()
        # False on historical data (no book exists), True in live where a
        # snapshot is available. Flipping this changes the strategy, so it is
        # explicit rather than inferred.
        self.require_depth = require_depth

    def execute(self, *, token_id: str, signal_ts: int, delay_secs: int,
                stake: float, reference_price: float,
                band: tuple | None = None,
                depth: float | None = None,
                spread: float | None = None,
                max_spread: float = 0.10) -> ExecutionResult:
        uncertainty: list = []
        st = self.st

        # 1. price discovery
        if delay_secs <= 0 or self.tape is None:
            price = reference_price
            uncertainty.append(
                "filled at the signal's own price; no reaction delay modelled")
        else:
            got = self.tape.price_at(token_id, signal_ts + delay_secs)
            if got is None:
                return ExecutionResult(
                    False, gate_key="x.unpriced",
                    reason=(f"nothing printed in {token_id[:12]} within the "
                            f"fill window after {delay_secs}s - this copy "
                            "could not have been executed at a knowable price"),
                    uncertainty=uncertainty)
            price = got

        # 2. spread
        if spread is not None and spread > max_spread:
            return ExecutionResult(
                False, gate_key="x.spread",
                reason=f"spread {spread:.3f} beyond {max_spread:.3f}",
                uncertainty=uncertainty)
        if spread is None:
            uncertainty.append(
                "spread unknown on this substrate; slippage assumption "
                f"({st.costs.slippage_bps:.0f}bps) stands in for it")

        # 3. depth
        state, why = self.depth.check(stake, depth)
        if state is DepthState.INSUFFICIENT:
            return ExecutionResult(False, gate_key="x.depth", reason=why,
                                   depth_state=state.value,
                                   uncertainty=uncertainty)
        if state is DepthState.UNKNOWN:
            uncertainty.append(why)
            if self.require_depth:
                return ExecutionResult(
                    False, gate_key="x.depth",
                    reason=("depth is required in this mode and no book "
                            "snapshot exists"),
                    depth_state=state.value, uncertainty=uncertainty)

        # 4. costs
        fill = st.costs.fill_price(price)
        slippage = fill - price

        # 5. did the price leave the strategy's own band while we were reacting
        if band is not None:
            lo, hi = band
            if not (lo <= fill <= hi):
                return ExecutionResult(
                    False, gate_key="x.price_moved",
                    reason=(f"executable price {fill:.3f} is outside the "
                            f"strategy's band [{lo:.2f}, {hi:.2f}] after "
                            f"{delay_secs}s - the setup is gone"),
                    depth_state=state.value, uncertainty=uncertainty)
        if not (st.costs.min_price < fill < st.costs.max_price):
            return ExecutionResult(
                False, gate_key="x.price_moved",
                reason=f"executable price {fill:.3f} outside global bounds",
                depth_state=state.value, uncertainty=uncertainty)

        stake = min(stake, st.costs.max_notional)
        uncertainty.append(
            "partial fills and market impact are not modelled; on this venue "
            "the copy's own size would move a thin book")
        return ExecutionResult(True, price=fill, stake=stake,
                               depth_state=state.value, slippage_paid=slippage,
                               uncertainty=uncertainty)


def cost_sensitivity(fills, st: Settings, extra_bps=(0, 25, 50, 100, 200)) -> list:
    """Does the edge survive worse execution than assumed?

    The cheapest way to find out that a strategy is really a spread-capture
    scheme. A result that dies at +50bps was never tradable, because 50bps is
    inside the uncertainty of every assumption above.
    """
    if not fills:
        return []
    out = []
    for bps in extra_bps:
        rets = []
        for f in fills:
            worse_entry = f.entry * (1 + bps / 10_000.0)
            exit_px = f.entry * (1 + f.ret)
            rets.append((exit_px - worse_entry) / worse_entry)
        n = len(rets)
        mean = sum(rets) / n
        wins = sum(1 for r in rets if r > 0)
        out.append({"extra_bps": bps, "expectancy": round(mean, 5),
                    "win_rate": round(wins / n, 4),
                    "survives": mean > 0})
    return out
