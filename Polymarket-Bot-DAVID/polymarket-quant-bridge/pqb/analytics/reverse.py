"""
Reverse-engineering one wallet's method from its public history.

The question this answers: *"Where are its losing exits? I can only see winners."*

There is nothing hidden. A Polymarket position can end in four ways, and only
one of them leaves a trade behind:

===================  =========================================================
**sold**             a SELL on the order book. Visible.
**held to a win**    the token redeems for $1.00 at resolution. No trade.
**held to a loss**   the token expires worth $0.00. **No trade, ever.**
**merged**           holding YES *and* NO lets the pair be redeemed for $1.00
                     through the contract. Not an order-book trade, so it
                     never appears in the tape either.
===================  =========================================================

So a wallet that buys and holds shows only its purchases. Its winners appear as
redemptions and its losers appear as nothing at all — which is exactly why a
profile filtered to settled positions looks like a trader who never loses.
Measured across observed wallets: **61% of those with five or more buys have
never sold anything.** They are not hiding exits. They have none.

This module reconstructs each position from the trade tape plus resolutions,
classifies how it ended, and describes the method in plain terms: where the
wallet enters, how long before resolution, whether it adds, whether it takes
profit early, and what it actually made.
"""

from __future__ import annotations

import collections
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

# How a position ended.
SOLD = "sold"
HELD_WIN = "held to a win"
HELD_LOSS = "held to a loss"
MERGED = "merged (both sides)"
OPEN = "still open"


@dataclass
class Position:
    """One wallet's whole involvement in one market, reconstructed."""

    market_id: str
    question: str = ""
    tokens: dict[str, float] = field(default_factory=dict)   # token -> net shares
    cost: float = 0.0            # net USDC put in (buys minus sell proceeds)
    gross_bought: float = 0.0
    proceeds: float = 0.0
    buys: int = 0
    sells: int = 0
    first_ts: int = 0
    last_ts: int = 0
    end_ts: Optional[int] = None
    entry_prices: list[float] = field(default_factory=list)
    outcome: str = OPEN
    settled_value: float = 0.0

    @property
    def both_sides(self) -> bool:
        return sum(1 for v in self.tokens.values() if v > 1e-6) >= 2

    @property
    def realized(self) -> float:
        """P&L once everything is accounted for: sales plus settlement."""
        return self.proceeds + self.settled_value - self.gross_bought

    @property
    def return_pct(self) -> float:
        return self.realized / self.gross_bought if self.gross_bought else 0.0

    @property
    def avg_entry(self) -> float:
        return (sum(self.entry_prices) / len(self.entry_prices)
                if self.entry_prices else 0.0)

    @property
    def hours_before_end(self) -> Optional[float]:
        if not self.end_ts or not self.first_ts:
            return None
        return max(0.0, (self.end_ts - self.first_ts) / 3600.0)


def reconstruct(rows: list[dict], resolutions: dict[str, float],
                end_times: Optional[dict[str, int]] = None
                ) -> list[Position]:
    """Rebuild every position this wallet took, and how each one ended."""
    end_times = end_times or {}
    by_market: dict[str, Position] = {}

    for row in sorted(rows, key=lambda r: int(r.get("ts") or 0)):
        market_id = str(row.get("market_id") or "")
        if not market_id:
            continue
        position = by_market.get(market_id)
        if position is None:
            position = Position(market_id=market_id,
                                question=str(row.get("question") or ""),
                                end_ts=end_times.get(market_id))
            by_market[market_id] = position

        token = str(row.get("token_id") or "")
        size = float(row.get("size") or 0.0)
        usdc = float(row.get("usdc") or 0.0)
        price = float(row.get("price") or 0.0)
        ts = int(row.get("ts") or 0)
        position.first_ts = position.first_ts or ts
        position.last_ts = max(position.last_ts, ts)

        if str(row.get("side") or "BUY").upper() == "BUY":
            position.tokens[token] = position.tokens.get(token, 0.0) + size
            position.gross_bought += usdc
            position.cost += usdc
            position.buys += 1
            if price > 0:
                position.entry_prices.append(price)
        else:
            position.tokens[token] = position.tokens.get(token, 0.0) - size
            position.proceeds += usdc
            position.cost -= usdc
            position.sells += 1

    for position in by_market.values():
        _classify(position, resolutions)
    return list(by_market.values())


def _classify(position: Position, resolutions: dict[str, float]) -> None:
    """Decide how this position ended, and what settlement was worth."""
    remaining = {t: s for t, s in position.tokens.items() if s > 1e-6}

    if not remaining:
        # Everything was sold back on the book.
        position.outcome = SOLD
        return

    known = {t: resolutions[t] for t in remaining if t in resolutions}
    if not known:
        position.outcome = OPEN
        return

    position.settled_value = sum(remaining[t] * v for t, v in known.items())

    if len(remaining) >= 2:
        # Both sides held to the end. Whether they merged early or redeemed at
        # resolution, the economics are the same and neither leaves a trade.
        position.outcome = MERGED
        return
    position.outcome = HELD_WIN if position.settled_value > 0 else HELD_LOSS


@dataclass
class Method:
    """What the wallet appears to actually do."""

    wallet: str
    positions: int = 0
    settled: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    invested: float = 0.0
    realized: float = 0.0
    win_rate: float = 0.0
    median_entry: float = 0.0
    entry_band: tuple[float, float] = (0.0, 0.0)
    median_hours_before_end: float = 0.0
    adds_rate: float = 0.0          # positions built over several buys
    early_exit_rate: float = 0.0    # positions sold rather than held
    both_sides_rate: float = 0.0
    by_price: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def describe(wallet: str, positions: list[Position]) -> Method:
    """Summarise the positions into a description of the method."""
    method = Method(wallet=wallet, positions=len(positions))
    if not positions:
        return method

    settled = [p for p in positions if p.outcome != OPEN]
    method.settled = len(settled)
    method.counts = dict(collections.Counter(p.outcome for p in positions))
    method.invested = sum(p.gross_bought for p in positions)
    method.realized = sum(p.realized for p in settled)

    wins = [p for p in settled if p.realized > 0]
    method.win_rate = len(wins) / len(settled) if settled else 0.0

    entries = [p.avg_entry for p in positions if p.avg_entry > 0]
    if entries:
        method.median_entry = statistics.median(entries)
        entries_sorted = sorted(entries)
        lo = entries_sorted[int(len(entries_sorted) * 0.15)]
        hi = entries_sorted[min(len(entries_sorted) - 1,
                                int(len(entries_sorted) * 0.85))]
        method.entry_band = (lo, hi)

    hours = [p.hours_before_end for p in positions
             if p.hours_before_end is not None]
    if hours:
        method.median_hours_before_end = statistics.median(hours)

    method.adds_rate = sum(1 for p in positions if p.buys > 1) / len(positions)
    method.early_exit_rate = (sum(1 for p in positions if p.sells > 0)
                              / len(positions))
    method.both_sides_rate = (sum(1 for p in positions if p.both_sides)
                              / len(positions))

    # Where the money is actually made, by entry price. This is the cut that
    # separates "buys cheap longshots" from "buys near-certainties" - two
    # completely different methods with completely different risk.
    for low, high in ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)):
        bucket = [p for p in settled if low <= p.avg_entry < high]
        if not bucket:
            continue
        invested = sum(p.gross_bought for p in bucket)
        method.by_price.append({
            "range": f"{low:.0%}-{high:.0%}", "n": len(bucket),
            "winRate": sum(1 for p in bucket if p.realized > 0) / len(bucket),
            "invested": invested,
            "realized": sum(p.realized for p in bucket),
            "returnPct": (sum(p.realized for p in bucket) / invested
                          if invested else 0.0),
        })

    method.notes = _notes(method)
    return method


def _notes(m: Method) -> list[str]:
    """Plain-English reading of the numbers."""
    notes: list[str] = []
    held = m.counts.get(HELD_WIN, 0) + m.counts.get(HELD_LOSS, 0)
    if m.positions and held / m.positions > 0.6:
        notes.append(
            f"Holds to resolution: {held / m.positions:.0%} of positions are "
            "never sold. Losers expire worthless and leave no trade behind, "
            "which is why its history looks like it only ever wins.")
    if m.early_exit_rate > 0.5:
        notes.append(
            f"Trades out early: sells before resolution in "
            f"{m.early_exit_rate:.0%} of positions, so it is managing exits "
            "rather than waiting for settlement.")
    if m.both_sides_rate > 0.15:
        notes.append(
            f"Runs both sides in {m.both_sides_rate:.0%} of markets. Holding "
            "YES and NO together can be redeemed for $1.00 through the "
            "contract, which is not an order-book trade - another reason its "
            "exits are invisible in the tape.")
    if m.adds_rate > 0.4:
        notes.append(
            f"Builds positions in stages: {m.adds_rate:.0%} are accumulated "
            "over several buys rather than taken in one go.")
    if m.median_entry and m.median_entry < 0.25:
        notes.append(
            f"Buys longshots (median entry {m.median_entry:.2f}). These lose "
            "most of the time and pay many times over when they land, so a "
            "low win rate here is expected and not a fault.")
    elif m.median_entry and m.median_entry > 0.7:
        notes.append(
            f"Buys favourites (median entry {m.median_entry:.2f}). Wins often, "
            "but each win is small and a single loss undoes several.")
    if m.median_hours_before_end:
        notes.append(
            f"Typically enters about {m.median_hours_before_end:.0f} hours "
            "before the market resolves.")
    if not notes:
        notes.append("Not enough settled history yet to characterise a method.")
    return notes
