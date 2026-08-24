"""
Layer 3: expected value, after everything it actually costs to trade.

On a prediction market a share pays exactly $1.00 or exactly $0.00, which makes
the arithmetic unusually clean. Buy at ask ``a`` with true probability ``p``:

    EV per share = p x (1 - a)  -  (1 - p) x a  =  p - a

So **the edge is simply our probability minus the price we can actually get**.
Everything else — spread, slippage, fees — is subtracted from that.

This is the gate the previous engine did not have. It compared a blended score
against a threshold and bought; nothing ever asked whether the price on offer
left any value in the trade. A "score" of 0.93 at an ask of 0.80 is not an
edge, and this module is what makes that statement checkable rather than
rhetorical.

Two consequences worth expecting:

* **Most candidates fail.** If our probability is close to the market's, there
  is no edge, and after costs the EV is negative. That is the correct answer
  and the reason to have the gate at all.
* **The bar rises as costs rise.** At a $0.19 trade a 1c fee each way is over
  10% round trip, so a trade must clear that before it is worth taking. The
  same trade at $5 needs only 0.4%.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models import MarketFeatures, OutcomeQuote
from .probability import ProbabilityEstimate


@dataclass
class EVConfig:
    """The bar a trade must clear, and what it is charged."""

    # Minimum expected value per dollar staked, after all costs. 2% is a
    # deliberately real bar: below it the estimate's own error dominates.
    min_ev: float = 0.02
    # How sure we must be before acting at all. Evidence from a handful of
    # trades cannot clear this, which is the point.
    min_confidence: float = 0.15
    # Assumed adverse move when taking liquidity, as a fraction of price.
    slippage: float = 0.01
    fee_per_trade_usdc: float = 0.01
    # A position is exited when its EV turns this negative — hysteresis, so a
    # position is not churned in and out around zero.
    exit_ev: float = -0.03


@dataclass
class Opportunity:
    """One candidate trade, priced and judged."""

    token_id: str
    market_id: str
    outcome: str
    question: str
    estimate: ProbabilityEstimate
    entry_price: float            # what we would actually pay
    ev_per_dollar: float          # after spread, slippage and fees
    stake: float = 0.0
    reject: str = ""              # why it was refused, "" = acceptable
    # Set when our order is larger than the visible resting size, so the
    # assumed fill is worse than the displayed ask.
    liquidity_note: str = ""

    @property
    def acceptable(self) -> bool:
        return not self.reject

    def to_dict(self) -> dict:
        return {
            "tokenId": self.token_id, "outcome": self.outcome,
            "entryPrice": round(self.entry_price, 4),
            "evPerDollar": round(self.ev_per_dollar, 5),
            "stake": round(self.stake, 4),
            "reject": self.reject,
            "liquidityNote": self.liquidity_note,
            **self.estimate.to_dict(),
        }


def round_trip_fee_usdc(market: Optional[MarketFeatures], stake: float,
                        cfg: EVConfig) -> float:
    """What it costs in fees to get in AND back out of a ``stake``-dollar trade.

    Prefers the market's own **taker** fee (basis points of notional) because we
    take liquidity with Fill-And-Kill orders — near mid-range a taker fee can be
    the difference between positive and negative EV, so assuming zero (as both
    reference bots do) would systematically overstate the edge. When the exchange
    publishes no rate, it falls back to the flat per-fill fee.

    NOTE: the exact metadata field for the fee rate must be verified against the
    live Polymarket docs; the parser in ``data_adapter`` reads several candidate
    names and leaves ``taker_fee_bps`` as ``None`` when none is present, which is
    exactly when this falls back to the configured flat fee.
    """
    if stake <= 0:
        return 0.0
    bps = market.taker_fee_bps if market is not None else None
    if bps is not None and bps > 0:
        # Proportional: bps/10_000 of the notional, charged on entry and exit.
        return (bps / 10_000.0) * stake * 2.0
    # Flat fallback: a fixed USDC amount per fill, both ways.
    return cfg.fee_per_trade_usdc * 2.0 if cfg.fee_per_trade_usdc > 0 else 0.0


def executable_price(quote: OutcomeQuote, stake: float) -> tuple[float, str]:
    """What we would actually pay for ``stake`` dollars, and any caveat.

    The displayed ask is the price of the *first* share, not of ours. A wallet
    may have entered at a price our order cannot reproduce, and an order larger
    than the resting size walks up the book.

    Only top-of-book size is published on a streamed book, so this does not
    pretend to walk a full ladder it cannot see. What it can do honestly is
    detect when our order exceeds the visible size and price the excess worse,
    rather than assuming unlimited liquidity at the touch — which is what
    quietly turns a modelled edge into a real loss.
    """
    ask = quote.ask or quote.mid or quote.last or 0.0
    if ask <= 0 or stake <= 0:
        return ask, ""

    wanted = stake / ask
    depth = quote.ask_depth
    if depth <= 0:
        # REST-topped books know the touch but not the size behind it. Say so
        # rather than assuming either way.
        return ask, "depth unknown (quote is not streamed)"
    if wanted <= depth:
        return ask, ""

    # The visible size fills at the ask; the rest is assumed to clear one tick
    # worse per further depth-worth of size. Deliberately crude, and
    # deliberately pessimistic.
    tick = quote.tick_size or 0.01
    overflow = wanted - depth
    levels = 1 + overflow / max(depth, 1e-9)
    blended = (depth * ask + overflow * (ask + tick * levels)) / wanted
    return min(0.99, blended), (
        f"order is {wanted:.0f} shares against {depth:.0f} visible; "
        f"assumed fill {blended:.3f} not {ask:.3f}")


def evaluate(quote: OutcomeQuote, market: MarketFeatures,
             estimate: ProbabilityEstimate, cfg: EVConfig,
             stake: float = 0.0) -> Opportunity:
    """Price a candidate and decide whether it is worth taking."""
    ask, liquidity_note = executable_price(quote, stake)
    opportunity = Opportunity(
        token_id=quote.token_id, market_id=market.market_id,
        outcome=quote.outcome, question=market.question, estimate=estimate,
        entry_price=ask, ev_per_dollar=0.0, stake=stake,
        liquidity_note=liquidity_note)

    if ask <= 0 or ask >= 1.0:
        opportunity.reject = "no tradable price"
        return opportunity

    # Gross edge: our probability against the price we must actually pay. The
    # ask already includes the spread, so the spread is not charged again.
    gross = estimate.probability - ask

    # Slippage: the book moves against size. Charged as a fraction of price.
    costs = ask * cfg.slippage
    # Fees: the per-market taker rate when the exchange publishes one, otherwise
    # the flat fallback. Weight depends on stake size — a fee on a 19-cent trade
    # is a different animal from the same fee on $5.
    if stake > 0:
        fee = round_trip_fee_usdc(market, stake, cfg)
        if fee > 0:
            costs += fee / stake   # in AND out, expressed per dollar staked

    opportunity.ev_per_dollar = (gross - costs) / ask

    if estimate.confidence < cfg.min_confidence:
        opportunity.reject = (
            f"not enough evidence (confidence {estimate.confidence:.2f} < "
            f"{cfg.min_confidence:.2f})")
    elif opportunity.ev_per_dollar < cfg.min_ev:
        opportunity.reject = (
            f"edge too small (EV {opportunity.ev_per_dollar:+.1%} < "
            f"{cfg.min_ev:.1%}) at ask {ask:.2f} vs our {estimate.probability:.2f}")
    return opportunity


def position_ev(current_price: float, estimate: ProbabilityEstimate,
                cfg: EVConfig) -> float:
    """EV per dollar of CONTINUING to hold, at the price we could exit at.

    Holding is a decision to buy at the current price all over again, so it is
    judged the same way. The difference is what we are charged: getting out
    costs one fee and one spread, so a position is not abandoned for a small
    negative — hence the separate, laxer ``exit_ev``.
    """
    price = min(max(current_price, 0.01), 0.99)
    return (estimate.probability - price) / price - cfg.slippage
