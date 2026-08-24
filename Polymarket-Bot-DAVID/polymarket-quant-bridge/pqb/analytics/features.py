"""
Wallet and market feature generation (step 3 of the analytical pipeline).

Turns raw observations into the measurements ranking and anomaly detection are
computed from. Nothing here decides anything or filters anyone out: every
observed wallet gets a profile, including the ones with two trades and no
measurable skill, because "this wallet has almost no history" is itself a
feature the engine may want.

**How a wallet's skill is measured.** A prediction market gives an unusually
clean answer. A wallet that bought an outcome at 0.30 which later resolved YES
paid 0.30 for something worth 1.00 — it was right, and by how much is not a
matter of opinion. So each trade is scored against the outcome's *eventual*
value: the settlement price where the market has resolved, and the current mark
where it has not. Resolved trades are the ground truth; marked ones are a
running estimate that firms up as markets settle.

Returns are clipped before they are averaged. A winning buy at 0.01 returns
+9,900%, and one such trade would otherwise dominate a wallet's entire average
and every population statistic derived from it — turning a single lottery
ticket into the highest-ranked wallet on the board.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..models import MarketIntel

# A single trade's return is clipped into this band before it is averaged.
# Asymmetric because the downside genuinely is bounded — a long outcome token
# can lose 100% and no more — while the upside is not.
RETURN_FLOOR = -1.0
RETURN_CEILING = 3.0

# Trades below this notional are treated as noise for *scoring* purposes: dust
# trades are frequently test transactions or rounding, and letting them into the
# skill average lets a wallet farm a record on trades it never risked anything
# on. They are still ingested, still counted, still available as features.
MIN_SCORING_USDC = 1.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: list[float]) -> float:
    """Population standard deviation. Zero for fewer than two samples."""
    if len(values) < 2:
        return 0.0
    mu = _mean(values)
    var = sum((v - mu) ** 2 for v in values) / len(values)
    return math.sqrt(max(0.0, var))


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


@dataclass
class TradeScore:
    """One trade judged against what the outcome turned out to be worth."""

    ts: int
    token_id: str
    market_id: str
    side: str
    price: float
    usdc: float
    value: float                    # settlement price, or the current mark
    ret: float                      # clipped
    win: bool
    resolved: bool                  # True = judged against a settled market


def net_book_score(rows: list[dict], resolutions: dict[str, float],
                   mark_fn: Optional[MarkFn] = None) -> Optional[TradeScore]:
    """Score a whole market book as ONE position, not as separate legs.

    This exists because of how the best wallets on Polymarket actually trade:
    they buy **both** outcomes of a binary market. If YES costs 0.45 and NO
    costs 0.52, the pair costs 0.97 to own something that is guaranteed to pay
    1.00 — a locked-in 3% with no risk whatsoever. Measured on real data, 55%
    of the both-sides books observed were priced under 1.00 like this.

    Scoring each leg on its own reads that as one win and one loss: a ~50% win
    rate and an average return near zero. The wallet looks mediocre when it is
    in fact running a riskless book. Netting the legs is the only way the
    figure means what it says.

    It also matters for trading, not just for reporting: a hedger's YES buy is
    not a directional opinion, so copying that leg alone takes the risk the
    wallet deliberately avoided.
    """
    net_shares: dict[str, float] = {}
    cost = 0.0
    for row in rows:
        token = str(row.get("token_id") or "")
        size = float(row.get("size") or 0.0)
        usdc = float(row.get("usdc") or 0.0)
        if str(row.get("side") or "BUY").upper() == "BUY":
            net_shares[token] = net_shares.get(token, 0.0) + size
            cost += usdc
        else:
            net_shares[token] = net_shares.get(token, 0.0) - size
            cost -= usdc
    if cost < MIN_SCORING_USDC:
        return None

    # Netting is done in SHARES, so a book with no share data cannot be valued.
    # Without this guard it would value at zero against a positive cost and
    # record a confident -100% — inventing a catastrophic loss out of a missing
    # field, which is far worse than declining to score it.
    if not any(abs(s) > 1e-9 for s in net_shares.values()):
        return None

    value = 0.0
    resolved = True
    for token, shares in net_shares.items():
        if abs(shares) < 1e-9:
            continue
        settled = resolutions.get(token)
        if settled is None:
            resolved = False
            settled = mark_fn(token) if mark_fn else None
            if settled is None or settled <= 0:
                return None
        value += shares * float(settled)

    ret = (value - cost) / cost
    first = rows[0]
    return TradeScore(
        ts=int(first.get("ts") or 0), token_id="",
        market_id=str(first.get("market_id") or ""), side="BOOK",
        price=cost, usdc=cost, value=value,
        ret=max(RETURN_FLOOR, min(RETURN_CEILING, ret)),
        win=ret > 0, resolved=resolved,
    )


@dataclass
class WalletProfile:
    """Everything measured about one wallet, before any ranking is applied."""

    wallet: str
    trades: int = 0
    markets: int = 0
    # How much of what this wallet does is hedged — both outcomes of the same
    # market held at once. High means its individual buys are not directional
    # opinions and must not be copied as if they were.
    hedged_books: int = 0
    total_books: int = 0
    first_seen: int = 0
    last_seen: int = 0
    avg_usdc: float = 0.0
    std_usdc: float = 0.0
    median_usdc: float = 0.0
    max_usdc: float = 0.0
    trades_per_day: float = 0.0
    scores: list[TradeScore] = field(default_factory=list)
    recent_sizes: list[float] = field(default_factory=list)
    entries: list[tuple[int, str]] = field(default_factory=list)  # (ts, token)

    # -- measured skill ------------------------------------------------------

    @property
    def sample(self) -> int:
        return len(self.scores)

    @property
    def resolved_sample(self) -> int:
        return sum(1 for s in self.scores if s.resolved)

    @property
    def win_rate(self) -> float:
        return (sum(1 for s in self.scores if s.win) / len(self.scores)
                if self.scores else 0.0)

    @property
    def avg_return(self) -> float:
        return _mean([s.ret for s in self.scores])

    @property
    def realized_usdc(self) -> float:
        """USDC-weighted edge — what the wallet's calls were actually worth.

        Weighted rather than averaged because a wallet that is right on its big
        trades and wrong on its small ones is a different wallet from one with
        the same win rate the other way round, and only the weighted figure can
        tell them apart.
        """
        return sum(s.ret * s.usdc for s in self.scores)

    @property
    def hedge_rate(self) -> float:
        """Fraction of this wallet's market books that hold both outcomes.

        The single most important thing to know before treating one of its
        buys as a directional signal: at a high rate, its buys are legs of a
        riskless pair, not an opinion about which side wins.
        """
        return self.hedged_books / self.total_books if self.total_books else 0.0

    def size_z(self, usdc: float) -> float:
        """How unusual a trade of this size is *for this wallet*.

        Uses the wallet's own median and dispersion, so "unusually large" means
        unusual relative to its own habits — the point of the detector — rather
        than large relative to the market.
        """
        if self.trades < 3 or self.std_usdc <= 0:
            return 0.0
        return (usdc - self.median_usdc) / self.std_usdc

    def to_dict(self) -> dict:
        return {
            "wallet": self.wallet, "trades": self.trades,
            "markets": self.markets, "sample": self.sample,
            "resolvedSample": self.resolved_sample,
            "winRate": round(self.win_rate, 4),
            "avgReturn": round(self.avg_return, 4),
            "realizedUsdc": round(self.realized_usdc, 2),
            "avgUsdc": round(self.avg_usdc, 2),
            "medianUsdc": round(self.median_usdc, 2),
            "stdUsdc": round(self.std_usdc, 2),
            "tradesPerDay": round(self.trades_per_day, 3),
            "firstSeen": self.first_seen, "lastSeen": self.last_seen,
        }


MarkFn = Callable[[str], Optional[float]]


def score_trade(row: dict, resolutions: dict[str, float],
                mark_fn: Optional[MarkFn] = None) -> Optional[TradeScore]:
    """Judge one trade against what its outcome turned out to be worth.

    Returns ``None`` when there is nothing to judge against — an unresolved
    market with no live mark. An unscoreable trade is not a zero-scoring trade,
    and treating it as one would drag every wallet's average toward zero in
    proportion to how many of its markets are still open.
    """
    price = float(row.get("price") or 0.0)
    usdc = float(row.get("usdc") or 0.0)
    if price <= 0 or usdc < MIN_SCORING_USDC:
        return None
    token_id = str(row.get("token_id") or "")

    # A settled value of 0.0 means "this outcome lost" — the single most
    # informative thing a market can tell us about a wallet. It must not be
    # confused with "no value known", or every losing trade disappears and
    # every wallet's record is silently reduced to the trades it won.
    value = resolutions.get(token_id)
    resolved = value is not None
    if not resolved and mark_fn is not None:
        mark = mark_fn(token_id)
        # A mark of zero is an absent book, not a valuation, so unlike a
        # settlement it is rejected rather than believed.
        if mark is not None and mark > 0:
            value = mark
    if value is None or value < 0:
        return None

    side = str(row.get("side") or "BUY").upper()
    if side == "BUY":
        ret = (value - price) / price
    else:
        # A sale is a bet the outcome is worth less than what was paid for it.
        # Normalised by the sale price so it is comparable with a buy's return.
        ret = (price - value) / price

    clipped = max(RETURN_FLOOR, min(RETURN_CEILING, ret))
    return TradeScore(
        ts=int(row.get("ts") or 0), token_id=token_id,
        market_id=str(row.get("market_id") or ""), side=side, price=price,
        usdc=usdc, value=float(value), ret=clipped, win=ret > 0,
        resolved=resolved,
    )


def build_profiles(rows: list[dict], resolutions: dict[str, float],
                   mark_fn: Optional[MarkFn] = None,
                   now: Optional[float] = None) -> dict[str, WalletProfile]:
    """Build one profile per observed wallet from raw trade rows.

    ``rows`` is every observation in the lookback window, for every wallet —
    the broad dataset, not a shortlist.
    """
    now = time.time() if now is None else now
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        wallet = str(row.get("wallet") or "").lower()
        if wallet:
            grouped.setdefault(wallet, []).append(row)

    profiles: dict[str, WalletProfile] = {}
    for wallet, trades in grouped.items():
        trades.sort(key=lambda r: int(r.get("ts") or 0))
        sizes = [float(t.get("usdc") or 0.0) for t in trades]
        first = int(trades[0].get("ts") or 0)
        last = int(trades[-1].get("ts") or 0)
        # Measured over the window the wallet has actually been observed for,
        # floored at an hour so a wallet first seen minutes ago does not read
        # as thousands of trades per day.
        span_days = max(3600.0, now - first) / 86400.0

        profile = WalletProfile(
            wallet=wallet,
            trades=len(trades),
            markets=len({str(t.get("market_id") or "") for t in trades} - {""}),
            first_seen=first, last_seen=last,
            avg_usdc=_mean(sizes), std_usdc=_stdev(sizes),
            median_usdc=_median(sizes), max_usdc=max(sizes) if sizes else 0.0,
            trades_per_day=len(trades) / span_days,
            recent_sizes=sizes[-50:],
            entries=[(int(t.get("ts") or 0), str(t.get("token_id") or ""))
                     for t in trades
                     if str(t.get("side") or "").upper() == "BUY"],
        )
        # Group into market books. A book where the wallet bought more than one
        # outcome is hedged and must be scored whole — see net_book_score.
        books: dict[str, list[dict]] = {}
        for row in trades:
            market = str(row.get("market_id") or "")
            if market:
                books.setdefault(market, []).append(row)
        profile.total_books = len(books)

        for market, rows in books.items():
            bought = {str(r.get("token_id") or "") for r in rows
                      if str(r.get("side") or "BUY").upper() == "BUY"}
            bought.discard("")
            if len(bought) >= 2:
                profile.hedged_books += 1
                score = net_book_score(rows, resolutions, mark_fn)
                if score is not None:
                    profile.scores.append(score)
                continue
            for row in rows:
                score = score_trade(row, resolutions, mark_fn)
                if score is not None:
                    profile.scores.append(score)

        # Trades with no market id cannot be booked; score them individually.
        for row in trades:
            if not str(row.get("market_id") or ""):
                score = score_trade(row, resolutions, mark_fn)
                if score is not None:
                    profile.scores.append(score)
        profiles[wallet] = profile
    return profiles


def market_intel(activity: list[dict], store, cohort: set[str],
                 cohort_activity: Optional[dict[str, dict]] = None,
                 now: Optional[float] = None) -> dict[str, MarketIntel]:
    """Per-market flow, each market measured against its own recent baseline.

    The z-score is deliberately per-market: $50k arriving in a market that
    normally sees $2k an hour is the event; the same $50k in a market that
    always sees $500k is not. A single global threshold would only ever fire on
    the biggest markets, which is where the least information is.
    """
    now = time.time() if now is None else now
    hour = int(now // 3600)
    out: dict[str, MarketIntel] = {}
    cohort_activity = cohort_activity or {}

    for row in activity:
        market_id = str(row.get("market_id") or "")
        if not market_id:
            continue
        gross = float(row.get("gross_usdc") or 0.0)
        history = store.market_baseline(market_id, before_hour=hour, hours=72)
        baseline = _mean(history)
        dispersion = _stdev(history)
        if len(history) >= 4 and dispersion > 0:
            flow_z = (gross - baseline) / dispersion
        else:
            flow_z = 0.0
        extra = cohort_activity.get(market_id, {})
        out[market_id] = MarketIntel(
            market_id=market_id,
            wallets_active=int(row.get("wallets") or 0),
            trades=int(row.get("trades") or 0),
            gross_usdc=gross,
            net_usdc=float(row.get("net_usdc") or 0.0),
            flow_z=flow_z,
            baseline_usdc=baseline,
            cohort_wallets=int(extra.get("wallets", 0)),
            cohort_net_usdc=float(extra.get("net_usdc", 0.0)),
        )
    return out
