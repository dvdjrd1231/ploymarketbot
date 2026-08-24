"""
Position lifecycle reconstruction: how a wallet MANAGES a market, not what it buys.

The sharper question, and the right one: don't ask *"what does this wallet
buy?"* — ask what it does to a position after it is on. These wallets treat a
market as **inventory**, not a one-shot bet. They enter, they add in stages, and
— the part that actually matters — at some point they start buying the *opposite*
outcome. Reconstructing that sequence is where the method actually lives.

So for every market a wallet touched, this walks the trades in time order and
extracts the lifecycle:

* **Entry** — the first buy: which outcome, at what price.
* **Adds** — the incremental buys that follow, and whether they are averaging
  *up* (adding as it moves their way) or *down* (adding as it moves against
  them). One entry per market is the exception here, not the rule.
* **The flip** — the first time they buy the OTHER outcome token in the same
  market. This is the signal. By comparing the entry outcome's price then versus
  at the flip, we can tell whether they flip because the position moved AGAINST
  them (defending / averaging the other side) or FOR them (locking in, hedging a
  winner). That distinction is the strategy.

And it refuses the "100% lossless" reading directly. A win rate counted over
resolved winners quietly ignores every dollar still tied up in open positions.
The cash-flow summary here separates money actually returned from money still at
risk, so a wallet that *looks* lossless because its losers have not resolved yet
is shown for what it is.
"""

from __future__ import annotations

import collections
import statistics
from dataclasses import dataclass, field
from typing import Optional

# The directional band David found to be the strongest simple signal. Entries
# here are broken out on their own so the claim is measurable rather than felt.
BAND_LO, BAND_HI = 0.60, 0.79


@dataclass
class MarketLife:
    """One wallet's whole lifecycle in one market, in event order."""

    market_id: str
    question: str = ""
    entry_token: str = ""
    entry_price: float = 0.0
    entry_ts: int = 0
    adds: int = 0                       # extra buys of the entry token
    add_prices: list[float] = field(default_factory=list)
    averaged_down: int = 0              # adds cheaper than the running avg
    averaged_up: int = 0               # adds richer than the running avg
    flipped: bool = False
    flip_ts: int = 0
    flip_opp_price: float = 0.0         # price paid for the opposite token
    flip_self_implied: float = 0.0      # entry outcome's implied price then (binary)
    flip_after_adds: int = 0
    tokens: dict[str, float] = field(default_factory=dict)   # token -> net shares
    gross_bought: float = 0.0
    proceeds: float = 0.0
    settled_value: float = 0.0
    resolved: bool = False

    @property
    def both_sides(self) -> bool:
        return sum(1 for v in self.tokens.values() if v > 1e-6) >= 2

    @property
    def open_cost(self) -> float:
        """Cost still exposed: bought minus what has come back so far."""
        if self.resolved:
            return 0.0
        return max(0.0, self.gross_bought - self.proceeds)

    @property
    def realized(self) -> float:
        return self.proceeds + self.settled_value - self.gross_bought

    @property
    def flip_defended(self) -> Optional[bool]:
        """True if the entry outcome had moved AGAINST them at the flip.

        Defending a loser (entry price fell before flipping into the other side)
        reads very differently from locking a winner (entry price rose). Only
        meaningful once we actually flipped and have both prices.
        """
        if not self.flipped or not self.entry_price or not self.flip_self_implied:
            return None
        return self.flip_self_implied < self.entry_price


def reconstruct(rows: list[dict], resolutions: dict[str, float]) -> list[MarketLife]:
    """Rebuild each market's lifecycle for one wallet, in time order."""
    by_market: dict[str, MarketLife] = {}
    running_cost: dict[str, dict[str, float]] = collections.defaultdict(
        lambda: {"cost": 0.0, "shares": 0.0})   # market -> entry-token running avg

    for row in sorted(rows, key=lambda r: int(r.get("ts") or 0)):
        market_id = str(row.get("market_id") or "")
        if not market_id:
            continue
        token = str(row.get("token_id") or "")
        side = str(row.get("side") or "BUY").upper()
        price = float(row.get("price") or 0.0)
        size = float(row.get("size") or 0.0)
        usdc = float(row.get("usdc") or 0.0)
        ts = int(row.get("ts") or 0)

        life = by_market.get(market_id)
        if life is None:
            life = MarketLife(market_id=market_id,
                              question=str(row.get("question") or ""))
            by_market[market_id] = life

        if side == "BUY":
            life.gross_bought += usdc
            life.tokens[token] = life.tokens.get(token, 0.0) + size
            if not life.entry_token:
                # First buy in this market: the entry.
                life.entry_token = token
                life.entry_price = price
                life.entry_ts = ts
                if price > 0:
                    acc = running_cost[market_id]
                    acc["cost"] += usdc
                    acc["shares"] += size
            elif token == life.entry_token:
                # An add to the same outcome.
                life.adds += 1
                if price > 0:
                    life.add_prices.append(price)
                    acc = running_cost[market_id]
                    avg = acc["cost"] / acc["shares"] if acc["shares"] else price
                    if price < avg:
                        life.averaged_down += 1
                    elif price > avg:
                        life.averaged_up += 1
                    acc["cost"] += usdc
                    acc["shares"] += size
            else:
                # A buy of the OTHER outcome — the flip.
                if not life.flipped:
                    life.flipped = True
                    life.flip_ts = ts
                    life.flip_opp_price = price
                    life.flip_after_adds = life.adds
                    # Binary market: the two outcomes price to ~$1 together, so
                    # the opposite token trading at p implies the entry outcome
                    # was worth ~(1 - p) at that moment.
                    if 0.0 < price < 1.0:
                        life.flip_self_implied = 1.0 - price
        else:
            life.proceeds += usdc
            life.tokens[token] = life.tokens.get(token, 0.0) - size

    for life in by_market.values():
        _settle(life, resolutions)
    return list(by_market.values())


def _settle(life: MarketLife, resolutions: dict[str, float]) -> None:
    remaining = {t: s for t, s in life.tokens.items() if s > 1e-6}
    known = {t: resolutions[t] for t in remaining if t in resolutions}
    if remaining and known and len(known) == len(remaining):
        life.settled_value = sum(remaining[t] * v for t, v in known.items())
        life.resolved = True
    elif not remaining:
        life.resolved = True     # fully sold back on the book


@dataclass
class LifecycleProfile:
    """What the wallet's position management actually looks like, in aggregate."""

    wallet: str
    markets: int = 0
    resolved: int = 0
    median_entry: float = 0.0
    adds_per_market: float = 0.0
    avg_down_rate: float = 0.0          # of all adds, share that averaged down
    flip_rate: float = 0.0             # markets where it bought both sides
    flips_defending: int = 0            # flipped after entry moved against it
    flips_locking: int = 0             # flipped after entry moved for it
    median_flip_entry: float = 0.0      # entry price of the markets it flipped in
    median_flip_self: float = 0.0       # implied entry-outcome price at the flip
    cash_in: float = 0.0
    cash_back: float = 0.0             # sells + redeemed winners
    still_at_risk: float = 0.0          # cost basis in unresolved positions
    realized: float = 0.0
    band: dict = field(default_factory=dict)   # the 60-79c cut
    notes: list[str] = field(default_factory=list)


def profile(wallet: str, lives: list[MarketLife]) -> LifecycleProfile:
    prof = LifecycleProfile(wallet=wallet, markets=len(lives))
    if not lives:
        return prof

    prof.resolved = sum(1 for l in lives if l.resolved)
    entries = [l.entry_price for l in lives if l.entry_price > 0]
    if entries:
        prof.median_entry = statistics.median(entries)

    total_adds = sum(l.adds for l in lives)
    prof.adds_per_market = total_adds / len(lives)
    down = sum(l.averaged_down for l in lives)
    prof.avg_down_rate = down / total_adds if total_adds else 0.0

    flipped = [l for l in lives if l.flipped]
    prof.flip_rate = len(flipped) / len(lives)
    prof.flips_defending = sum(1 for l in flipped if l.flip_defended is True)
    prof.flips_locking = sum(1 for l in flipped if l.flip_defended is False)
    # Compare like with like: entry price and implied-at-flip price over the SAME
    # flipped markets, so the two medians describe one population, not two.
    comparable = [l for l in flipped
                  if l.entry_price > 0 and l.flip_self_implied > 0]
    if comparable:
        prof.median_flip_entry = statistics.median(l.entry_price for l in comparable)
        prof.median_flip_self = statistics.median(
            l.flip_self_implied for l in comparable)

    prof.cash_in = sum(l.gross_bought for l in lives)
    prof.cash_back = sum(l.proceeds + l.settled_value for l in lives)
    prof.still_at_risk = sum(l.open_cost for l in lives)
    prof.realized = sum(l.realized for l in lives if l.resolved)

    # The 60-79c band, on its own, measured rather than asserted.
    band = [l for l in lives if BAND_LO <= l.entry_price <= BAND_HI]
    band_resolved = [l for l in band if l.resolved]
    if band:
        invested = sum(l.gross_bought for l in band)
        made = sum(l.realized for l in band_resolved)
        prof.band = {
            "n": len(band), "resolved": len(band_resolved),
            "winRate": (sum(1 for l in band_resolved if l.realized > 0)
                        / len(band_resolved)) if band_resolved else 0.0,
            "invested": invested, "realized": made,
            "returnPct": made / invested if invested else 0.0,
        }

    prof.notes = _notes(prof)
    return prof


def _notes(p: LifecycleProfile) -> list[str]:
    notes: list[str] = []
    if p.adds_per_market >= 1.0:
        notes.append(
            f"Manages inventory, not bets: {p.adds_per_market:.1f} adds per "
            "market on average, building each position in stages rather than "
            "taking it in one go.")
    if p.avg_down_rate > 0.55:
        notes.append(
            f"Averages DOWN: {p.avg_down_rate:.0%} of adds are below the running "
            "cost, so it leans into positions moving against it rather than "
            "cutting them.")
    if p.flip_rate > 0.15:
        detail = ""
        if p.flips_defending or p.flips_locking:
            if p.flips_defending >= p.flips_locking:
                detail = (" - usually AFTER the entry moved against it, i.e. "
                          "adding the other side to defend a losing position")
            else:
                detail = (" - usually after the entry moved in its favour, i.e. "
                          "locking a winner by taking the other side")
        notes.append(
            f"Flips into the opposite outcome in {p.flip_rate:.0%} of markets"
            f"{detail}. Holding both sides can be redeemed for $1 through the "
            "contract, which never shows up as a sell.")
    if p.still_at_risk > 0.05 * p.cash_in and p.cash_in > 0:
        notes.append(
            f"NOT lossless - ${p.still_at_risk:,.0f} of the ${p.cash_in:,.0f} it "
            f"put in ({p.still_at_risk / p.cash_in:.0%}) is still tied up in "
            "unresolved positions. A win rate counted only over resolved "
            "winners hides every one of those dollars.")
    if p.band.get("resolved"):
        b = p.band
        notes.append(
            f"60-79c entries: {b['resolved']} resolved, {b['winRate']:.0%} won, "
            f"{b['returnPct']:+.1%} on ${b['invested']:,.0f} - the band you "
            "flagged, measured on this wallet.")
    if not notes:
        notes.append("Not enough lifecycle history yet to characterise "
                     "position management.")
    return notes
