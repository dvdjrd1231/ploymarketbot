"""Executable prices and exits — and what the data honestly supports.

Part 20 is the part most easily faked. A backtest that assumes every signal
could be filled at the mid is not a pessimistic backtest, it is a different
strategy that nobody can trade. So this module answers two questions with
explicit provenance on every answer:

    what could we have PAID at time t?      -> entry_price()
    what would the position be WORTH?       -> exit_value()

Three price sources exist in this store and they are not interchangeable:

* **BOOK** — `research_rows` carries bid/ask/depth for ~2.5k tokens over a few
  days. This is a real quote and the only source that supports depth and
  spread. Best quality, smallest coverage.
* **PRINT** — the wallet tape itself. Somebody traded at that price, which is
  much better evidence than a model and much worse than a quote: it is one
  side of a trade at an unknown size, and the spread around it is unknown.
* **SETTLEMENT** — `resolutions.price`, 1.0 or 0.0. Ground truth, available
  for a small minority of markets.

Every fill records which source priced it. Every report states the mix. A
result computed mostly from PRINT prices under the OPTIMISTIC assumption is
not the same claim as one computed from BOOK prices under CONSERVATIVE, and
the difference is reported rather than averaged away.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

BOOK = "book"
PRINT = "print"
SETTLEMENT = "settlement"
UNAVAILABLE = "unavailable"


@dataclass
class Quote:
    """One market observation, with its provenance attached."""

    token_id: str = ""
    ts: float = 0.0
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    depth: Optional[float] = None
    source: str = UNAVAILABLE
    age_seconds: float = 0.0

    @property
    def available(self) -> bool:
        return self.source != UNAVAILABLE

    @property
    def spread(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    def to_dict(self) -> dict:
        return {"ts": self.ts, "bid": self.bid, "ask": self.ask,
                "mid": self.mid, "depth": self.depth, "source": self.source,
                "ageSeconds": round(self.age_seconds, 1)}


@dataclass(frozen=True)
class ExecutionAssumption:
    """One named set of execution beliefs. Never one number, always three.

    `assumed_half_spread` is what we charge on top of a PRINT price to stand
    in for the ask we cannot see. It is the single most consequential number
    in the whole P&L result, which is exactly why it is a named, varied
    assumption rather than a constant buried in a function.
    """

    name: str
    assumed_half_spread: float      # charged when no book is available
    slippage: float                 # extra, as a fraction of price
    fee_per_trade_usdc: float
    latency_seconds: float          # how stale the price we act on may be
    max_fraction_of_depth: float    # of visible depth, when depth is known
    allow_print_pricing: bool = True

    def to_dict(self) -> dict:
        return {"name": self.name,
                "assumedHalfSpread": self.assumed_half_spread,
                "slippage": self.slippage,
                "feePerTradeUsdc": self.fee_per_trade_usdc,
                "latencySeconds": self.latency_seconds,
                "maxFractionOfDepth": self.max_fraction_of_depth,
                "allowPrintPricing": self.allow_print_pricing}


# Part 20: three assumptions, always all three, never only the flattering one.
OPTIMISTIC = ExecutionAssumption(
    "OPTIMISTIC", assumed_half_spread=0.005, slippage=0.0,
    fee_per_trade_usdc=0.0, latency_seconds=0.0, max_fraction_of_depth=1.0)
BASE = ExecutionAssumption(
    "BASE", assumed_half_spread=0.015, slippage=0.005,
    fee_per_trade_usdc=0.01, latency_seconds=5.0, max_fraction_of_depth=0.5)
CONSERVATIVE = ExecutionAssumption(
    "CONSERVATIVE", assumed_half_spread=0.030, slippage=0.015,
    fee_per_trade_usdc=0.02, latency_seconds=30.0, max_fraction_of_depth=0.25)
ASSUMPTIONS = (OPTIMISTIC, BASE, CONSERVATIVE)


@dataclass
class Fill:
    """What a simulated order actually achieved, or why it did not."""

    filled: bool = False
    price: float = 0.0
    shares: float = 0.0
    usdc: float = 0.0
    fee: float = 0.0
    slippage_cost: float = 0.0
    price_source: str = UNAVAILABLE
    quote_age_seconds: float = 0.0
    partial: bool = False
    requested_usdc: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return {"filled": self.filled, "price": round(self.price, 6),
                "shares": round(self.shares, 6), "usdc": round(self.usdc, 6),
                "fee": round(self.fee, 6),
                "slippageCost": round(self.slippage_cost, 6),
                "priceSource": self.price_source,
                "quoteAgeSeconds": round(self.quote_age_seconds, 1),
                "partial": self.partial,
                "requestedUsdc": round(self.requested_usdc, 4),
                "reason": self.reason}


class PriceOracle:
    """Prices for a token at a time, from the best source that exists.

    Series are loaded lazily per token and cached. That matters: the tape has
    713k rows and the research pass asks about thousands of tokens, so a
    per-query scan would turn a minute into an hour, and loading everything up
    front would hold the whole tape in memory twice.
    """

    def __init__(self, intel_path: str | Path,
                 print_tolerance_seconds: float = 3_600.0,
                 book_tolerance_seconds: float = 300.0):
        self.path = Path(intel_path)
        self.print_tolerance = float(print_tolerance_seconds)
        self.book_tolerance = float(book_tolerance_seconds)
        self._conn: Optional[sqlite3.Connection] = None
        self._books: dict[str, list] = {}
        self._prints: dict[str, list] = {}
        self._settlements: Optional[dict[str, float]] = None
        self.stats: dict[str, int] = {}
        if self.path.exists():
            self._conn = sqlite3.connect(f"file:{self.path}?mode=ro",
                                         uri=True, timeout=30.0)
            self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- sources ------------------------------------------------------------

    def _book_series(self, token_id: str) -> list:
        if token_id in self._books:
            return self._books[token_id]
        series: list = []
        if self._conn is not None:
            try:
                rows = self._conn.execute(
                    "SELECT ts, features FROM research_rows WHERE token_id=? "
                    "ORDER BY ts", (token_id,)).fetchall()
            except sqlite3.Error:
                rows = []
            for row in rows:
                try:
                    payload = json.loads(row["features"] or "{}")
                except (TypeError, ValueError):
                    continue
                bid = _positive(payload.get("bid"))
                ask = _positive(payload.get("ask"))
                if not bid and not ask:
                    continue
                series.append((float(row["ts"]), bid or None, ask or None,
                               _positive(payload.get("mid")) or None,
                               _positive(payload.get("ask_depth")) or None))
        self._books[token_id] = series
        return series

    def _print_series(self, token_id: str) -> list:
        if token_id in self._prints:
            return self._prints[token_id]
        series: list = []
        if self._conn is not None:
            try:
                rows = self._conn.execute(
                    "SELECT ts, price, size FROM wallet_trades "
                    "WHERE token_id=? AND price > 0 ORDER BY ts",
                    (token_id,)).fetchall()
            except sqlite3.Error:
                rows = []
            series = [(float(r["ts"]), float(r["price"]), float(r["size"] or 0))
                      for r in rows]
        self._prints[token_id] = series
        return series

    def settlement(self, token_id: str) -> Optional[float]:
        if self._settlements is None:
            self._settlements = {}
            if self._conn is not None:
                try:
                    for row in self._conn.execute(
                            "SELECT token_id, price FROM resolutions"):
                        self._settlements[str(row["token_id"])] = \
                            float(row["price"] or 0.0)
                except sqlite3.Error:
                    pass
        return self._settlements.get(str(token_id))

    # -- the two questions --------------------------------------------------

    def quote_at(self, token_id: str, ts: float) -> Quote:
        """The best observation at or BEFORE `ts`.

        Strictly at-or-before. A quote from after the signal is future
        information, and using "the nearest observation" would silently import
        it whenever the next capture happened to be closer than the last.
        """
        book = _nearest_before(self._book_series(token_id), ts,
                               self.book_tolerance)
        if book is not None:
            stamp, bid, ask, mid, depth = book
            self._bump("book")
            return Quote(token_id=token_id, ts=stamp, bid=bid, ask=ask,
                         mid=(mid if mid is not None
                              else _mid_of(bid, ask)),
                         depth=depth, source=BOOK, age_seconds=ts - stamp)
        printed = _nearest_before(self._print_series(token_id), ts,
                                  self.print_tolerance)
        if printed is not None:
            stamp, price, _size = printed
            self._bump("print")
            return Quote(token_id=token_id, ts=stamp, bid=None, ask=None,
                         mid=price, depth=None, source=PRINT,
                         age_seconds=ts - stamp)
        self._bump("unavailable")
        return Quote(token_id=token_id, ts=ts, source=UNAVAILABLE)

    def latest_quote(self, token_id: str, not_before: float = 0.0) -> Quote:
        """The NEWEST observation for a token — the mark for an open position.

        Deliberately not `quote_at(far_future)`. That path applies a staleness
        tolerance, which is right for "what could we have paid at 14:03" and
        wrong for "what is this worth now": a position whose token last
        printed six hours ago is not unpriceable, it is priced at that print,
        and rejecting it silently converted every unresolved position into
        'no exit available' and deleted it from the P&L.

        `not_before` refuses a mark that predates the entry, because a mark
        older than the position is a statement about a different moment.
        """
        book = self._book_series(token_id)
        printed = self._print_series(token_id)
        candidates = []
        if book:
            stamp, bid, ask, mid, depth = book[-1]
            candidates.append((stamp, Quote(
                token_id=token_id, ts=stamp, bid=bid, ask=ask,
                mid=(mid if mid is not None else _mid_of(bid, ask)),
                depth=depth, source=BOOK)))
        if printed:
            stamp, price, _size = printed[-1]
            candidates.append((stamp, Quote(
                token_id=token_id, ts=stamp, mid=price, source=PRINT)))
        if not candidates:
            self._bump("unavailable")
            return Quote(token_id=token_id, ts=not_before, source=UNAVAILABLE)
        stamp, quote = max(candidates, key=lambda item: item[0])
        if not_before and stamp < not_before:
            self._bump("stale_mark")
            return Quote(token_id=token_id, ts=stamp, source=UNAVAILABLE)
        self._bump(f"mark_{quote.source}")
        return quote

    def buy(self, token_id: str, ts: float, stake_usdc: float,
            assumption: ExecutionAssumption) -> Fill:
        """Simulate BUYING `stake_usdc` of a token at `ts`.

        The price we pay is built up explicitly so every component is
        auditable: the ask if a real book gave us one, otherwise the print plus
        an ASSUMED half spread, then slippage, then the fee. A price above 0.99
        is refused — there is nothing to win there and a fill at 0.999 would
        manufacture a risk-free-looking return out of rounding.
        """
        fill = Fill(requested_usdc=stake_usdc)
        if stake_usdc <= 0:
            fill.reason = "no stake"
            return fill
        # Latency: we act on the world as it was `latency` ago, then pay at a
        # price we could only have learned after deciding.
        quote = self.quote_at(token_id, ts - assumption.latency_seconds)
        if not quote.available:
            fill.reason = ("no executable price: neither a captured book nor a "
                           "trade print exists for this token near the signal")
            return fill
        if quote.source == PRINT and not assumption.allow_print_pricing:
            fill.reason = ("no captured order book, and this assumption "
                           "refuses to price from prints")
            return fill

        if quote.ask is not None and quote.ask > 0:
            raw = quote.ask
        else:
            base = quote.mid or quote.bid
            if not base:
                fill.reason = "quote carried no usable price"
                return fill
            raw = base * (1.0 + assumption.assumed_half_spread)
        price = raw * (1.0 + assumption.slippage)
        if price >= 0.99:
            fill.reason = (f"executable price {price:.4f} leaves no room — "
                           "refused rather than booked as a near-certain win")
            return fill
        if price <= 0.0:
            fill.reason = "non-positive executable price"
            return fill

        spendable = stake_usdc
        partial = False
        if quote.depth is not None and quote.depth > 0:
            allowed = quote.depth * assumption.max_fraction_of_depth * price
            if allowed < spendable:
                spendable, partial = allowed, True
        fee = assumption.fee_per_trade_usdc
        if spendable <= fee:
            fill.reason = (f"stake ${spendable:.2f} does not cover the "
                           f"${fee:.2f} fee")
            return fill
        shares = (spendable - fee) / price
        if shares <= 0:
            fill.reason = "stake rounds to zero shares"
            return fill

        fill.filled = True
        fill.price = price
        fill.shares = shares
        fill.usdc = spendable
        fill.fee = fee
        fill.slippage_cost = shares * (price - raw)
        fill.price_source = quote.source
        fill.quote_age_seconds = quote.age_seconds
        fill.partial = partial
        fill.reason = ("filled at the captured ask" if quote.ask
                       else "filled at a print plus an ASSUMED half spread")
        return fill

    def exit_value(self, token_id: str, after_ts: float,
                   shares: float, assumption: ExecutionAssumption
                   ) -> tuple[float, str, float]:
        """What the position was worth, and how we know.

        Returns `(value_usdc, basis, price)`. `basis` is `settlement` when the
        market resolved, otherwise `mark` — and the two are NEVER summed in a
        report. A settled result is a realised fact; a mark is an opinion about
        an open position, and merging them would let unresolved winners be
        booked as profit.
        """
        settled = self.settlement(token_id)
        if settled is not None:
            return shares * settled, SETTLEMENT, settled
        quote = self.latest_quote(token_id, after_ts)
        if not quote.available:
            return 0.0, UNAVAILABLE, 0.0
        # Selling hits the bid, and where no book exists the print is marked
        # DOWN by the same assumed half spread the buy was marked up by.
        if quote.bid is not None and quote.bid > 0:
            price = quote.bid
        else:
            base = quote.mid or 0.0
            price = base * (1.0 - assumption.assumed_half_spread)
        price = max(0.0, price * (1.0 - assumption.slippage))
        value = shares * price - assumption.fee_per_trade_usdc
        return max(0.0, value), "mark", price

    def _bump(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

    def provenance(self) -> dict:
        entries = sum(self.stats.get(k, 0)
                      for k in ("book", "print", "unavailable")) or 1
        return {"lookups": sum(self.stats.values()),
                "bySource": dict(self.stats),
                # Entry pricing only. Marks are counted separately because
                # they answer a different question and mixing them would make
                # the entry mix look better than it is.
                "entryLookups": entries,
                "bookShare": round(self.stats.get("book", 0) / entries, 4),
                "printShare": round(self.stats.get("print", 0) / entries, 4),
                "unavailableShare": round(
                    self.stats.get("unavailable", 0) / entries, 4),
                "markBook": self.stats.get("mark_book", 0),
                "markPrint": self.stats.get("mark_print", 0),
                "markStale": self.stats.get("stale_mark", 0)}


def _positive(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _mid_of(bid, ask) -> Optional[float]:
    if bid and ask:
        return (bid + ask) / 2.0
    return bid or ask or None


def _nearest_before(series: list, ts: float, tolerance: float):
    """The last entry at or before `ts`, if it is within `tolerance`.

    Binary search: these series run to thousands of points and this is called
    once per episode per horizon per assumption.
    """
    if not series:
        return None
    lo, hi = 0, len(series) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= ts:
            best = series[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        return None
    return best if (ts - best[0]) <= tolerance else None


