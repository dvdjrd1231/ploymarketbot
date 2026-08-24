"""
Order sizing and exchange-rule validation.

Polymarket rejects an order that breaks tick size, minimum size, or the 0-1
price domain. Discovering that from a rejection costs a round trip and, worse,
leaves the engine believing it acted when it did not — so every order is made
legal here, before submission, from the rules published in the market's own
metadata rather than from assumed constants.

Deliberately pure functions over plain numbers: this is the code path that
decides how much money moves, so it must be unit-testable without a network,
an exchange, or a config object.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# Price can never be 0 or 1: those are settlement values, not quotes.
_EPS = 1e-9
SHARE_DECIMALS = 2


@dataclass
class SizedOrder:
    """The result of sizing. ``ok`` False means: do not submit, and why."""

    ok: bool
    price: float = 0.0
    shares: float = 0.0
    usdc: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "price": self.price, "shares": self.shares,
                "usdc": self.usdc, "reason": self.reason}


def _clean_tick(tick: float) -> float:
    tick = float(tick or 0.0)
    return tick if tick > 0 else 0.01


def round_price_up(price: float, tick: float) -> float:
    """Round to the next tick at or above ``price`` (the price a buyer pays).

    Rounding a buy limit *down* would place it below the ask and fail to fill,
    which reads as "no liquidity" when the real cause is arithmetic.
    """
    tick = _clean_tick(tick)
    steps = math.ceil(price / tick - _EPS)
    return _bounded(round(steps * tick, 6), tick)


def round_price_down(price: float, tick: float) -> float:
    """Round to the next tick at or below ``price`` (what a seller accepts)."""
    tick = _clean_tick(tick)
    steps = math.floor(price / tick + _EPS)
    return _bounded(round(steps * tick, 6), tick)


def _bounded(price: float, tick: float) -> float:
    return max(tick, min(round(1.0 - tick, 6), price))


def floor_shares(shares: float, decimals: int = SHARE_DECIMALS) -> float:
    factor = 10 ** decimals
    return math.floor(max(0.0, shares) * factor + _EPS) / factor


def effective_min_usdc(config_min: float, progression_min: float) -> float:
    """The binding minimum notional.

    Two independent floors apply: the operator's configured minimum and the
    current step of the doubling-rule progression. The larger wins — the
    progression is meant to *raise* stake as the account grows, so it must not
    be undercut by a stale config value.
    """
    return max(float(config_min or 0.0), float(progression_min or 0.0))


def fee_drag(stake_usdc: float, fee_per_trade: float) -> float:
    """Round-trip fee as a fraction of the stake.

    Doubled because a position is paid for twice: once to open, once to close.
    A flat fee is regressive in exactly the wrong place for this system — the
    progression *starts* at $0.19, where a 1c fee is 5.3% each way, so the
    cheapest trades carry by far the heaviest drag.
    """
    if stake_usdc <= 0:
        return float("inf")
    return (2.0 * max(0.0, fee_per_trade)) / stake_usdc


def size_buy(desired_usdc: float, ask: Optional[float], tick: float,
             available_cash: float, min_usdc: float,
             market_min_shares: float = 0.0,
             reserve_fraction: float = 0.0,
             max_price: float = 0.99) -> SizedOrder:
    """Turn a dollar intent into a legal BUY.

    ``ask`` is the executable price; the limit is placed there (rounded up to a
    tick) so a Fill-And-Kill either trades at that price or better, or not at
    all. Cash is checked against the *rounded* notional, not the intent, because
    rounding up the price raises the bill.
    """
    if ask is None or ask <= 0:
        return SizedOrder(False, reason="No executable ask price.")
    price = round_price_up(float(ask), tick)
    if price > max_price + _EPS:
        return SizedOrder(False, price=price,
                          reason=f"Price {price:.3f} above the {max_price:.2f} cap.")

    spendable = max(0.0, float(available_cash) * (1.0 - max(0.0, reserve_fraction)))
    notional = min(float(desired_usdc), spendable)
    if notional <= 0:
        return SizedOrder(False, price=price,
                          reason="No spendable cash after the reserve.")

    shares = floor_shares(notional / price)
    if shares <= 0:
        return SizedOrder(False, price=price,
                          reason=f"${notional:.2f} at {price:.3f} rounds to zero shares.")
    if market_min_shares and shares < market_min_shares:
        # Try to meet the exchange's floor if the cash is there; otherwise skip.
        needed = market_min_shares * price
        if needed <= spendable + _EPS:
            shares = floor_shares(market_min_shares)
            if shares < market_min_shares:
                shares = round(market_min_shares, SHARE_DECIMALS)
        else:
            return SizedOrder(
                False, price=price,
                reason=f"Exchange minimum {market_min_shares:g} shares needs "
                       f"${needed:.2f}, only ${spendable:.2f} spendable.")

    cost = round(shares * price, 6)
    if cost > spendable + _EPS:
        shares = floor_shares(spendable / price)
        cost = round(shares * price, 6)
    if shares <= 0:
        return SizedOrder(False, price=price, reason="Insufficient cash.")
    if cost < min_usdc - _EPS:
        return SizedOrder(
            False, price=price, shares=shares, usdc=cost,
            reason=f"${cost:.2f} is below the ${min_usdc:.2f} minimum.")
    return SizedOrder(True, price=price, shares=shares, usdc=cost)


def size_sell(shares_held: float, bid: Optional[float], tick: float,
              fraction: float = 1.0,
              market_min_shares: float = 0.0) -> SizedOrder:
    """Turn an exit/reduce intent into a legal SELL.

    An exit is never blocked by a minimum-notional rule: leaving a position open
    because it became small is how a losing tail is kept forever. A *reduce*
    that would leave an unsellable dust remainder is promoted to a full exit for
    the same reason.
    """
    if shares_held <= 0:
        return SizedOrder(False, reason="Nothing held.")
    if bid is None or bid <= 0:
        return SizedOrder(False, reason="No executable bid price.")
    price = round_price_down(float(bid), tick)
    fraction = min(1.0, max(0.0, float(fraction)))
    if fraction <= 0:
        return SizedOrder(False, price=price, reason="Zero reduction fraction.")

    shares = floor_shares(shares_held * fraction)
    if shares <= 0:
        return SizedOrder(False, price=price,
                          reason="Reduction rounds to zero shares.")

    remainder = round(shares_held - shares, SHARE_DECIMALS)
    if market_min_shares:
        if shares < market_min_shares:
            shares = floor_shares(min(shares_held, market_min_shares))
            remainder = round(shares_held - shares, SHARE_DECIMALS)
        if 0 < remainder < market_min_shares:
            # The leftover could not be sold later — close the whole thing now.
            shares = floor_shares(shares_held)
            remainder = 0.0
    if shares <= 0:
        return SizedOrder(False, price=price, reason="Nothing sellable.")
    return SizedOrder(True, price=price, shares=shares,
                      usdc=round(shares * price, 6))
