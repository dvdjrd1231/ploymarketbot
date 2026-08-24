"""
The per-wallet playbook: what exit rule would actually have worked.

The brief: *"reverse engineer every top wallet, and adjust the exits to give
the optimal algorithm for every single ranked wallet."*

The important thing this is **not** doing is copying their exits. Most of these
wallets have no exits to copy — 61% of observed wallets with five or more buys
have never sold anything. They buy and hold to settlement, so their losers
expire silently and there is no exit decision in the record at all.

So the question is turned around. Given that this wallet entered *here*, and
given how the price actually moved afterwards, **what would the best exit rule
have been for us?** That is answerable, because the trade tape is a price
history: every trade anyone made in that market is an observation of the price
at a moment in time.

Each candidate rule is replayed against the real path of every position the
wallet took, and scored on what it would have returned. The winner is that
wallet's playbook entry.

Two guards against fooling ourselves:

* **A rule must beat simply holding**, which is the wallet's own default. If
  nothing beats holding, that is the honest answer and it is reported as such.
* **A rule needs a minimum number of positions** behind it. The best-looking
  rule over four trades is noise, and presenting it as a strategy would be the
  same error as trusting a wallet with a 100% record over eleven trades.
"""

from __future__ import annotations

import collections
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Optional

# A wallet needs at least this many settled positions with a usable price path
# before any rule is fitted to it.
MIN_POSITIONS = 5

# Price observations needed after entry before a path is usable. Below this we
# cannot tell whether a take-profit level was ever actually reached.
MIN_PATH_POINTS = 4


@dataclass
class ExitRule:
    """One candidate way of getting out."""

    name: str
    take_profit: Optional[float] = None   # exit at +X% on cost
    stop_loss: Optional[float] = None     # exit at -Y% on cost
    trail: Optional[float] = None         # exit when price falls X% from its peak
    max_hold_hours: Optional[float] = None  # exit this long after entry regardless
    hold: bool = False                    # ride it to settlement

    def describe(self) -> str:
        if self.hold:
            return "hold to resolution"
        parts = []
        if self.take_profit is not None:
            parts.append(f"take profit at +{self.take_profit:.0%}")
        if self.trail is not None:
            parts.append(f"trail {self.trail:.0%} off peak")
        if self.stop_loss is not None:
            parts.append(f"cut at -{self.stop_loss:.0%}")
        if self.max_hold_hours is not None:
            parts.append(f"time-exit {self.max_hold_hours:.0f}h")
        return ", ".join(parts) or "hold"


def default_rules() -> list[ExitRule]:
    """The grid searched for every wallet.

    Beyond the fixed take-profit / stop-loss levels, two families answer David's
    "spike then fade" observation directly:

    * **trailing stops** let a position ride a sudden move up and only exit once
      it gives back a set fraction of the peak — that is how you *capture* an
      impulse instead of either selling into it early or watching it round-trip
      back to entry.
    * **time exits** get out a set number of hours after entry regardless of
      price, for the case where the edge is the early move and everything after
      it is just resolution risk you were never paid to hold.
    """
    rules = [ExitRule("hold", hold=True)]
    for tp in (0.10, 0.20, 0.35, 0.50, 1.00):
        rules.append(ExitRule(f"tp{int(tp*100)}", take_profit=tp))
    for sl in (0.20, 0.35, 0.50):
        rules.append(ExitRule(f"sl{int(sl*100)}", stop_loss=sl))
    for tp in (0.20, 0.35, 0.50):
        for sl in (0.25, 0.50):
            rules.append(ExitRule(f"tp{int(tp*100)}/sl{int(sl*100)}",
                                  take_profit=tp, stop_loss=sl))
    # Spike capture: ride the move, exit when it gives back `trail` of the peak.
    for tr in (0.10, 0.20, 0.35):
        rules.append(ExitRule(f"trail{int(tr*100)}", trail=tr))
    # Impulse-and-out: take profit if it comes, but leave after a fixed window.
    for hrs in (6.0, 24.0, 72.0):
        rules.append(ExitRule(f"tp20/{int(hrs)}h", take_profit=0.20,
                              max_hold_hours=hrs))
    return rules


@dataclass
class Trial:
    """One position replayed under one rule."""

    market_id: str
    entry: float
    exit_price: float
    settled: float
    ret: float
    exited_early: bool


def simulate(entry: float, path: list[tuple[int, float]], settled: float,
             rule: ExitRule) -> tuple[float, float, bool]:
    """Replay one position. Returns (exit price, return, exited early).

    The path is walked in order. Within a single observation the checks run in a
    deliberately conservative order — stop, then trailing stop, then take-profit,
    then time — so that when two levels are crossed between observations and we
    cannot know which came first, the LESS favourable one is assumed. Assuming
    the favourable one would quietly inflate every result in this module.

    Trailing and time rules need a clock. The path is ``(ts, price)`` and its
    first point is at or just after entry, so elapsed time is measured from
    there; if timestamps are missing the trailing rule still works on price
    alone and the time rule simply never triggers.
    """
    if entry <= 0:
        return settled, 0.0, False
    if rule.hold or not path:
        return settled, (settled - entry) / entry, False

    stop_at = entry * (1 - rule.stop_loss) if rule.stop_loss is not None else None
    target_at = (entry * (1 + rule.take_profit)
                 if rule.take_profit is not None else None)
    t0 = path[0][0]
    peak = entry

    for ts, price in path:
        if price <= 0:
            continue
        if stop_at is not None and price <= stop_at:
            return price, (price - entry) / entry, True
        # Trailing stop: only arms once the position is in profit, so it never
        # turns into a second, tighter stop-loss below the entry.
        peak = max(peak, price)
        if rule.trail is not None and peak > entry:
            if price <= peak * (1 - rule.trail):
                return price, (price - entry) / entry, True
        if target_at is not None and price >= target_at:
            return price, (price - entry) / entry, True
        if (rule.max_hold_hours is not None and ts > t0
                and (ts - t0) / 3600.0 >= rule.max_hold_hours):
            return price, (price - entry) / entry, True
    return settled, (settled - entry) / entry, False


@dataclass
class RuleResult:
    rule: ExitRule
    n: int = 0
    total_return: float = 0.0
    wins: int = 0
    early: int = 0
    worst: float = 0.0
    returns: list[float] = field(default_factory=list)

    @property
    def mean_return(self) -> float:
        return self.total_return / self.n if self.n else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def score(self) -> float:
        """Mean return penalised by how bumpy it was.

        A rule that averages +8% with a -90% worst case is not better than one
        that averages +6% smoothly, and ranking on the average alone would pick
        the first every time. This is the "controlling drawdown" half of the
        objective, expressed as a number rather than a hope.
        """
        if not self.returns:
            return 0.0
        spread = statistics.pstdev(self.returns) if len(self.returns) > 1 else 0.0
        return self.mean_return - 0.35 * spread

    def to_dict(self) -> dict:
        return {"rule": self.rule.describe(), "n": self.n,
                "meanReturn": round(self.mean_return, 4),
                "winRate": round(self.win_rate, 4),
                "earlyExits": self.early, "worst": round(self.worst, 4),
                "score": round(self.score, 4)}


@dataclass
class Playbook:
    wallet: str
    positions: int = 0
    best: Optional[RuleResult] = None
    hold: Optional[RuleResult] = None
    ranked: list[RuleResult] = field(default_factory=list)
    note: str = ""

    @property
    def improvement(self) -> float:
        if not self.best or not self.hold:
            return 0.0
        return self.best.mean_return - self.hold.mean_return


def build(wallet: str, positions: Iterable[dict],
          rules: Optional[list[ExitRule]] = None,
          min_positions: int = MIN_POSITIONS) -> Playbook:
    """Find the exit rule that would have served this wallet's entries best.

    ``positions`` are dicts of ``entry``, ``path`` and ``settled``.
    """
    rules = rules or default_rules()
    usable = [p for p in positions
              if p.get("entry", 0) > 0 and p.get("settled") is not None
              and len(p.get("path") or []) >= MIN_PATH_POINTS]
    book = Playbook(wallet=wallet, positions=len(usable))

    if len(usable) < min_positions:
        book.note = (f"only {len(usable)} usable position(s); needs "
                     f"{min_positions} before a rule means anything")
        return book

    results: list[RuleResult] = []
    for rule in rules:
        result = RuleResult(rule=rule)
        for position in usable:
            _price, ret, early = simulate(
                position["entry"], position["path"], position["settled"], rule)
            result.n += 1
            result.total_return += ret
            result.returns.append(ret)
            result.wins += 1 if ret > 0 else 0
            result.early += 1 if early else 0
            result.worst = min(result.worst, ret)
        results.append(result)

    results.sort(key=lambda r: r.score, reverse=True)
    book.ranked = results
    book.best = results[0]
    book.hold = next((r for r in results if r.rule.hold), None)

    if book.hold and book.best.rule.hold:
        book.note = ("nothing beat simply holding to resolution — this wallet's "
                     "own default is already the best rule for its entries")
    elif book.hold:
        book.note = (f"{book.best.rule.describe()} beats holding by "
                     f"{book.improvement:+.1%} per position")
    return book


def build_segmented(wallet: str, positions: Iterable[dict],
                    rules: Optional[list[ExitRule]] = None,
                    min_positions: int = MIN_POSITIONS
                    ) -> dict[str, Playbook]:
    """One playbook per market category for a single wallet.

    This is the "each wallet runs several algorithms" idea made concrete: the
    wallet's positions are split by market category (sports, politics, crypto,
    …) and a separate best-exit rule is fitted to each. A segment with too few
    positions is still returned, carrying the honest note that it has not enough
    history — better to show the gap than to fit a rule to three trades.

    Positions must carry a ``question`` so they can be categorised; ones without
    fall into ``other``.
    """
    from . import segments

    books: dict[str, Playbook] = {}
    for category, group in segments.split(positions).items():
        books[category] = build(wallet, group, rules=rules,
                                 min_positions=min_positions)
    return books


def price_paths(rows: list[dict]) -> dict[str, list[tuple[int, float]]]:
    """token -> chronological (ts, price) from every observed trade on it.

    Every trade anybody made is an observation of the price at that moment, so
    the tape doubles as a price history. It is not a full order book, but it is
    real traded prices rather than a model of them.
    """
    paths: dict[str, list[tuple[int, float]]] = collections.defaultdict(list)
    for row in rows:
        token = str(row.get("token_id") or "")
        price = float(row.get("price") or 0.0)
        ts = int(row.get("ts") or 0)
        if token and price > 0:
            paths[token].append((ts, price))
    for token in paths:
        paths[token].sort()
    return paths
