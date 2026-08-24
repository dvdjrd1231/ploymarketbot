"""
The data structures that cross the adapter <-> bridge boundary.

These are deliberately plain dataclasses with ``to_dict()``: they are written to
the decision journal several times a cycle and read by the analysis script, so
they need to serialise cheaply and stay readable in SQLite.

Anything Polymarket-specific stops here. The decision engine sees markets,
quotes, positions and wallet signals — not order books and condition ids.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class Action(str, Enum):
    """The six outcomes the decision engine may emit (see prompt section 4)."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    NOTHING = "DO_NOTHING"


class MarketStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    RESOLVED = "resolved"


# --- inbound: market data ---------------------------------------------------

@dataclass
class OutcomeQuote:
    """Top-of-book plus depth for one outcome token (prices are 0.00-1.00)."""

    token_id: str
    outcome: str = ""
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    last: Optional[float] = None
    spread: Optional[float] = None
    bid_depth: float = 0.0          # shares resting on the bid side
    ask_depth: float = 0.0
    tick_size: float = 0.01
    source: str = "none"            # "stream" | "rest" | "none"
    updated_ts: float = 0.0

    @property
    def mark(self) -> Optional[float]:
        """The price a position in this token should be valued at.

        The bid is what a holder could actually realise, so it is preferred;
        the midpoint is the fallback when one side of the book is empty. A thin
        book must never mark a position at 0 — that would read as a total loss
        when it is only an absent quote.
        """
        if self.bid and self.bid > 0:
            return self.bid
        if self.mid and self.mid > 0:
            return self.mid
        if self.last and self.last > 0:
            return self.last
        return None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MarketFeatures:
    """Everything the engine knows about one market at one instant."""

    market_id: str                  # Polymarket condition id
    question: str = ""
    slug: str = ""
    category: str = ""
    status: MarketStatus = MarketStatus.ACTIVE
    end_ts: Optional[int] = None
    quotes: dict[str, OutcomeQuote] = field(default_factory=dict)
    liquidity: float = 0.0
    volume_total: float = 0.0
    volume_24h: float = 0.0
    recent_trades: list[dict] = field(default_factory=list)
    outcome_prices: list[float] = field(default_factory=list)
    # Exchange trading rules, read from market metadata rather than assumed —
    # an order that violates either is rejected, so both are enforced before
    # submission (see ``adapters.sizing``).
    tick_size: float = 0.01
    min_order_size: float = 0.0     # in shares; 0 = not published
    # Per-market fees in basis points, read from metadata rather than assumed.
    # Our orders take liquidity (Fill-And-Kill), so the taker rate is the one
    # that bites; the maker rate is kept for completeness. ``None`` means the
    # exchange did not publish one and the flat fallback fee is used instead.
    taker_fee_bps: Optional[float] = None
    maker_fee_bps: Optional[float] = None
    # When the market was created/listed (unix ts). Age separates a brand-new
    # market finding its price from an old one suddenly repricing — the same
    # impulse means different things in each.
    created_ts: Optional[int] = None
    ts: float = field(default_factory=time.time)

    @property
    def seconds_to_resolution(self) -> Optional[float]:
        if not self.end_ts:
            return None
        return max(0.0, self.end_ts - time.time())

    @property
    def tradable(self) -> bool:
        return self.status is MarketStatus.ACTIVE

    def quote(self, token_id: str) -> Optional[OutcomeQuote]:
        return self.quotes.get(str(token_id))

    def to_dict(self) -> dict:
        return {
            "marketId": self.market_id,
            "question": self.question,
            "slug": self.slug,
            "category": self.category,
            "status": self.status.value,
            "endTs": self.end_ts,
            "secondsToResolution": self.seconds_to_resolution,
            "liquidity": self.liquidity,
            "volumeTotal": self.volume_total,
            "volume24h": self.volume_24h,
            "tickSize": self.tick_size,
            "minOrderSize": self.min_order_size,
            "quotes": {k: v.to_dict() for k, v in self.quotes.items()},
            "recentTrades": self.recent_trades[:20],
            "ts": self.ts,
        }


# --- inbound: my account ----------------------------------------------------

@dataclass
class PositionView:
    """One open position, as the engine sees it.

    ``peak_price``/``trough_price`` are maintained across cycles by the runner,
    because trailing-drawdown exits need the path a position took, not just its
    current mark.
    """

    token_id: str
    market_id: str = ""
    outcome: str = ""
    question: str = ""
    size: float = 0.0
    avg_price: float = 0.0
    cur_price: float = 0.0
    opened_ts: int = 0
    end_ts: Optional[int] = None
    peak_price: float = 0.0
    trough_price: float = 0.0
    lifecycle_id: Optional[int] = None
    source: str = "exchange"        # "exchange" | "simulated"

    @property
    def cost(self) -> float:
        return self.size * self.avg_price

    @property
    def market_value(self) -> float:
        return self.size * (self.cur_price or self.avg_price)

    @property
    def unrealized_pnl(self) -> float:
        return (self.cur_price - self.avg_price) * self.size if self.size else 0.0

    @property
    def return_pct(self) -> float:
        return (self.unrealized_pnl / self.cost) if self.cost else 0.0

    @property
    def drawdown_from_peak(self) -> float:
        """Fraction given back from the best mark this position ever showed."""
        if not self.peak_price or self.peak_price <= 0:
            return 0.0
        return max(0.0, (self.peak_price - self.cur_price) / self.peak_price)

    @property
    def seconds_to_resolution(self) -> Optional[float]:
        if not self.end_ts:
            return None
        return max(0.0, self.end_ts - time.time())

    def to_dict(self) -> dict:
        return {
            "tokenId": self.token_id,
            "marketId": self.market_id,
            "outcome": self.outcome,
            "question": self.question,
            "size": self.size,
            "avgPrice": self.avg_price,
            "curPrice": self.cur_price,
            "cost": self.cost,
            "marketValue": self.market_value,
            "unrealizedPnl": self.unrealized_pnl,
            "returnPct": self.return_pct,
            "peakPrice": self.peak_price,
            "drawdownFromPeak": self.drawdown_from_peak,
            "openedTs": self.opened_ts,
            "endTs": self.end_ts,
            "secondsToResolution": self.seconds_to_resolution,
            "lifecycleId": self.lifecycle_id,
            "source": self.source,
        }


@dataclass
class AccountState:
    address: str = ""
    balance: float = 0.0            # free USDC
    position_value: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    updated_ts: float = field(default_factory=time.time)

    @property
    def portfolio_value(self) -> float:
        return self.balance + self.position_value

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "balance": self.balance,
            "positionValue": self.position_value,
            "portfolioValue": self.portfolio_value,
            "realizedPnl": self.realized_pnl,
            "unrealizedPnl": self.unrealized_pnl,
            "updatedTs": self.updated_ts,
        }


# --- inbound: target wallets (a signal, never a command) --------------------

@dataclass
class WalletSignal:
    """Something a tracked wallet did. Weighted input, not an instruction."""

    wallet: str
    label: str = ""
    weight: float = 1.0
    action: str = "ENTRY"           # "ENTRY" | "EXIT"
    token_id: str = ""
    market_id: str = ""
    outcome: str = ""
    price: float = 0.0
    size: float = 0.0
    usdc: float = 0.0
    timestamp: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WalletPosition:
    wallet: str
    label: str = ""
    weight: float = 1.0
    token_id: str = ""
    market_id: str = ""
    outcome: str = ""
    size: float = 0.0
    avg_price: float = 0.0
    value: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# --- inbound: the analytics layer's view of wallets and markets --------------
#
# Ingestion is deliberately broad — every wallet observed trading a tracked
# market is recorded and keeps its identity. Ranking is then something the
# analytical layer *derives*, not something the config asserts, and the top
# cohort is a label on a wallet rather than a filter applied to the feed. A
# wallet outside the cohort still reaches the engine with its full profile, so
# an early or unusual signal from an unranked address is discoverable.

@dataclass
class WalletTrade:
    """One observed trade by any wallet. The normalised unit of ingestion.

    This is what the raw Data API rows are flattened into before anything else
    looks at them, so every downstream feature reads one shape regardless of
    which endpoint the row arrived from.
    """

    wallet: str
    ts: int = 0
    market_id: str = ""
    token_id: str = ""
    outcome: str = ""
    side: str = "BUY"               # "BUY" | "SELL"
    price: float = 0.0
    size: float = 0.0               # shares
    usdc: float = 0.0
    question: str = ""
    tx: str = ""
    source: str = "market"          # "market" | "global" | "tracked"

    @property
    def signed_usdc(self) -> float:
        return self.usdc if self.side == "BUY" else -self.usdc

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WalletIntel:
    """A wallet's derived profile: identity, behaviour, and measured skill.

    ``score`` is the dynamic rank score the analytics layer computes from
    historical predictive performance; ``rank`` is its position in that
    ordering. ``in_cohort`` marks membership of the top-N analytical cohort —
    an important feature, never a gate.
    """

    wallet: str
    label: str = ""
    rank: int = 0                   # 1 = best; 0 = unranked (too little data)
    score: float = 0.0              # 0-1, shrunk toward the population mean
    raw_score: float = 0.0          # before shrinkage, for diagnostics
    # What the ORDERING is done on: score discounted by how much evidence
    # stands behind it (confidence) and how many INDEPENDENT markets it
    # spans. A hot eight-trade streak scores well and ranks poorly, which
    # is the honest way round. See analytics.ranking.rank_wallets.
    rank_score: float = 0.0
    confidence: float = 0.0         # 0-1, how much sample stands behind it
    sample: int = 0                 # resolved/marked trades scored
    trades: int = 0
    markets: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0         # per-trade, on cost
    realized_usdc: float = 0.0
    avg_usdc: float = 0.0
    std_usdc: float = 0.0
    max_usdc: float = 0.0
    trades_per_day: float = 0.0
    # Share of this wallet's market books that hold BOTH outcomes at once.
    # A high value means its buys are legs of a hedged, near-riskless pair
    # rather than a view on which side wins — so they must not be copied as
    # directional signals. See analytics.features.net_book_score.
    hedge_rate: float = 0.0
    first_seen: int = 0
    last_seen: int = 0
    in_cohort: bool = False
    pinned: bool = False            # named in the config; seeds, never restricts

    @property
    def weight(self) -> float:
        """The blend weight this wallet's evidence carries.

        Derived from measured performance and sample size, so a wallet earns
        its influence rather than being assigned it in a config file. A pinned
        wallet keeps a floor so an operator's explicit interest is never
        silently ignored while its history is still thin.
        """
        earned = self.score * self.confidence
        # A hedger's individual leg carries little directional information, so
        # its influence on a BUY decision fades as its hedge rate rises. It can
        # still be an excellent wallet — it is simply not saying what a
        # one-sided buyer is saying.
        earned *= max(0.15, 1.0 - self.hedge_rate)
        return max(0.35, earned) if self.pinned else earned

    def to_dict(self) -> dict:
        data = asdict(self)
        data["weight"] = round(self.weight, 4)
        return data


@dataclass
class MarketIntel:
    """Per-market flow, measured against that market's own recent baseline."""

    market_id: str
    wallets_active: int = 0
    trades: int = 0
    gross_usdc: float = 0.0
    net_usdc: float = 0.0           # buys minus sells, in USDC
    flow_z: float = 0.0             # vs this market's own history
    baseline_usdc: float = 0.0
    cohort_wallets: int = 0         # top-cohort wallets active here
    cohort_net_usdc: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnomalySignal:
    """Something the analytics layer found unusual, with the evidence for it.

    Every anomaly is persisted, so "the system can demonstrate that its
    detected anomalies" is answerable after the fact rather than a claim.
    """

    kind: str                       # see analytics.anomalies.KINDS
    subject: str = ""               # wallet address or market id
    wallet: str = ""
    label: str = ""
    market_id: str = ""
    token_id: str = ""
    outcome: str = ""
    z: float = 0.0                  # in standard deviations, where meaningful
    strength: float = 0.0           # 0-1, comparable across kinds
    detail: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


# --- the bundle handed to the engine each cycle -----------------------------

@dataclass
class BridgeContext:
    """One evaluation cycle's complete world view."""

    cycle_id: str
    ts: float
    account: AccountState
    markets: dict[str, MarketFeatures] = field(default_factory=dict)
    positions: list[PositionView] = field(default_factory=list)
    wallet_signals: list[WalletSignal] = field(default_factory=list)
    wallet_positions: list[WalletPosition] = field(default_factory=list)
    min_trade_size: float = 0.0     # from the doubling-rule progression
    flattening: bool = False        # something is closing the whole book out
    # WHY we are flattening: "doubling" | "kill_switch". Both close everything,
    # but they are different events, and the journal groups exits by style — so
    # recording a kill-switch flatten as a doubling exit would misattribute
    # every one of those closes in the performance report.
    flatten_reason: str = ""
    portfolio_drawdown: float = 0.0
    # How many wallets are configured, which is not the same as how many sent a
    # signal this cycle. An engine that blends a wallet term must be able to
    # tell "no wallets are being tracked" (the term is undefined) from "the
    # tracked wallets did nothing" (the term is genuinely zero).
    tracked_wallets: int = 0

    # --- the analytics layer's output ---------------------------------------
    # Derived, not configured. ``wallet_intel`` is keyed by lowercase address
    # and covers every wallet observed, ranked or not. ``anomalies`` is this
    # cycle's detections. ``performance`` is what the journal says has actually
    # worked, so the engine can weigh its own history.
    wallet_intel: dict[str, WalletIntel] = field(default_factory=dict)
    market_intel: dict[str, MarketIntel] = field(default_factory=dict)
    anomalies: list[AnomalySignal] = field(default_factory=list)
    performance: dict[str, Any] = field(default_factory=dict)
    observed_wallets: int = 0       # total in the intel store, ranked or not
    # The non-print engine's structural snapshot per token (np_-prefixed
    # columns), computed by the runner's feed each cycle. Empty when the
    # engine is unavailable or a token has not printed yet.
    nonprint: dict[str, dict[str, float]] = field(default_factory=dict)
    # Market-wide regime summary (analytics.regime): the same four numbers for
    # every token this cycle — sizing scales by its aggressiveness, and the
    # columns are captured so discovery can learn whether regime predicts.
    regime: dict[str, float] = field(default_factory=dict)
    # Per-token wallet-tape summary for THIS cycle, from the observed trade
    # feed: how many distinct wallets printed and how concentrated the
    # notional was in the largest of them. Same construction as the
    # historical dataset's WalletConcentration.
    tape_wallets: dict[str, dict] = field(default_factory=dict)
    # The Market-State layer's per-token snapshot (ms_-prefixed columns):
    # rolling changes, impulse/exhaustion/anomaly scores and the
    # DORMANT..REVERSAL classification. LEAN consumes this instead of
    # reconstructing it.
    market_state: dict[str, dict[str, float]] = field(default_factory=dict)
    # Liquidation-cascade pressure this cycle (analytics.cascade): liq_*
    # columns, the same values for every token, so discovery can learn
    # whether derivatives forced flow predicts Polymarket movement.
    cascade: dict[str, float] = field(default_factory=dict)
    # The capital-preservation stake multiplier for this cycle, from
    # `riskpolicy.evaluate`. Always in (0, 1]: it can shrink what the engine
    # decided and can never grow it, so an engine that ignores it trades
    # exactly as it did before and an engine that honours it can only be more
    # conservative. Never a signal, never a view — a cap.
    capital_scale: float = 1.0
    capital_notes: list = field(default_factory=list)

    def market_for(self, position: PositionView) -> Optional[MarketFeatures]:
        return self.markets.get(position.market_id)

    def signals_for(self, token_id: str) -> list[WalletSignal]:
        return [s for s in self.wallet_signals if s.token_id == str(token_id)]

    def intel_for(self, wallet: str) -> Optional[WalletIntel]:
        return self.wallet_intel.get(str(wallet).lower())

    def anomalies_for(self, token_id: str = "",
                      market_id: str = "") -> list[AnomalySignal]:
        """Anomalies attached to a token, or to its market as a whole.

        A market-level anomaly (unusual capital arriving) is evidence about
        every outcome in that market, so it is returned for a token query too.
        """
        token, market = str(token_id), str(market_id)
        out = []
        for a in self.anomalies:
            if token and a.token_id == token:
                out.append(a)
            elif market and a.market_id == market and not a.token_id:
                out.append(a)
        return out

    def to_dict(self) -> dict:
        return {
            "cycleId": self.cycle_id,
            "ts": self.ts,
            "account": self.account.to_dict(),
            "markets": len(self.markets),
            "positions": len(self.positions),
            "walletSignals": len(self.wallet_signals),
            "walletsObserved": self.observed_wallets,
            "walletsRanked": sum(1 for w in self.wallet_intel.values() if w.rank),
            "anomalies": len(self.anomalies),
            "minTradeSize": self.min_trade_size,
            "flattening": self.flattening,
            "portfolioDrawdown": self.portfolio_drawdown,
        }


# --- outbound: decisions ----------------------------------------------------

@dataclass
class Decision:
    """One engine verdict, with the rationale that produced it.

    ``rationale`` is free-form and goes to the journal verbatim; it is what the
    analysis script later groups on to answer "which reasoning actually made
    money".
    """

    action: Action
    token_id: str = ""
    market_id: str = ""
    outcome: str = ""
    question: str = ""
    size_usdc: float = 0.0
    size_shares: float = 0.0
    limit_price: Optional[float] = None
    confidence: float = 0.0
    score: float = 0.0
    reason: str = ""
    rationale: dict[str, Any] = field(default_factory=dict)
    lifecycle_id: Optional[int] = None
    wallet_influence: str = ""      # label(s) of wallets that moved the score
    exit_style: str = ""            # take_profit | stop | trailing | wallet | time
    ts: float = field(default_factory=time.time)
    journal_id: Optional[int] = None

    @property
    def is_actionable(self) -> bool:
        return self.action in (Action.BUY, Action.SELL, Action.REDUCE,
                               Action.EXIT)

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "tokenId": self.token_id,
            "marketId": self.market_id,
            "outcome": self.outcome,
            "question": self.question,
            "sizeUsdc": self.size_usdc,
            "sizeShares": self.size_shares,
            "limitPrice": self.limit_price,
            "confidence": self.confidence,
            "score": self.score,
            "reason": self.reason,
            "rationale": self.rationale,
            "lifecycleId": self.lifecycle_id,
            "walletInfluence": self.wallet_influence,
            "exitStyle": self.exit_style,
            "ts": self.ts,
        }


@dataclass
class ExecutionReport:
    """What actually happened when a decision was sent to the exchange."""

    decision: Decision
    submitted: bool = False
    order_id: str = ""
    client_order_id: str = ""       # our idempotency key; stable across retries
    status: str = ""                # FILLED | PARTIAL | REJECTED | SIMULATED …
    requested_size: float = 0.0
    filled_size: float = 0.0
    avg_price: float = 0.0
    fee: float = 0.0
    error: str = ""
    simulated: bool = False
    ts: float = field(default_factory=time.time)

    @property
    def filled_usdc(self) -> float:
        return self.filled_size * self.avg_price

    @property
    def ok(self) -> bool:
        return self.submitted and self.filled_size > 0

    def to_dict(self) -> dict:
        return {
            "submitted": self.submitted,
            "orderId": self.order_id,
            "clientOrderId": self.client_order_id,
            "status": self.status,
            "requestedSize": self.requested_size,
            "filledSize": self.filled_size,
            "avgPrice": self.avg_price,
            "filledUsdc": self.filled_usdc,
            "fee": self.fee,
            "error": self.error,
            "simulated": self.simulated,
            "ts": self.ts,
        }


# --- journal tagging --------------------------------------------------------

def liquidity_bucket(liquidity: float) -> str:
    if liquidity >= 100_000:
        return "deep"
    if liquidity >= 20_000:
        return "normal"
    if liquidity >= 5_000:
        return "thin"
    return "illiquid"


def ttr_bucket(seconds: Optional[float]) -> str:
    """Time-to-resolution bucket. Resolution proximity dominates behaviour in
    prediction markets, so it is a first-class tag on every journal record."""
    if seconds is None:
        return "unknown"
    hours = seconds / 3600.0
    if hours <= 1:
        return "<1h"
    if hours <= 6:
        return "1-6h"
    if hours <= 24:
        return "6-24h"
    if hours <= 24 * 7:
        return "1-7d"
    if hours <= 24 * 30:
        return "7-30d"
    return ">30d"
