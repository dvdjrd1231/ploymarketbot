"""
Shared data models.

Ported from the desktop application's ``core/models.py`` with two additions the
web UI needs:

  * :class:`OrderRecord` — every order we send is tracked so the dashboard can
    show *pending* orders and their live exchange status, not just filled ones.
  * ``to_dict()`` on every model — the WebSocket hub serialises these several
    times per second, so we hand-roll plain dicts instead of paying Pydantic
    validation cost on the hot path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeStatus(str, Enum):
    OPEN = "OPEN"          # copied and currently held
    CLOSED = "CLOSED"      # market settled / position exited
    SKIPPED = "SKIPPED"    # detected but not copied (a rule filtered it out)
    FAILED = "FAILED"      # attempted but the order failed


class OrderStatus(str, Enum):
    PENDING = "PENDING"      # submitted, awaiting an exchange response
    LIVE = "LIVE"            # resting on the book (unfilled)
    MATCHED = "MATCHED"      # partially or fully matched
    FILLED = "FILLED"        # fully filled
    CANCELED = "CANCELED"    # killed / cancelled (FAK remainder, or by us)
    FAILED = "FAILED"        # rejected by the exchange


# --- Trading mode constants -------------------------------------------------

MODE_PROPORTIONAL = "proportional"   # Mode 1
MODE_FIXED_50 = "fixed_50"           # Mode 2 (primary)


@dataclass
class TargetTrade:
    """A trade observed on the *target* wallet's activity feed."""

    trade_id: str                 # stable unique id from the activity feed
    market_id: str                # condition id of the market
    token_id: str                 # ERC-1155 token id (the specific outcome)
    outcome: str                  # e.g. "Yes" / "No"
    side: Side
    price: float                  # target's entry price (0-1)
    size: float                   # number of shares
    usdc_size: float              # notional in USDC (price * size)
    timestamp: int                # unix seconds
    market_question: str = ""
    market_end_ts: Optional[int] = None  # settlement time (unix seconds)
    slug: str = ""

    @property
    def dollars(self) -> float:
        return self.usdc_size


@dataclass
class CopiedTrade:
    """A trade the bot placed (or attempted) on *your* account."""

    target_trade_id: str
    market_id: str
    token_id: str
    outcome: str
    market_question: str
    side: Side
    mode: str
    target_price: float
    target_usdc: float
    my_price: float = 0.0
    my_size: float = 0.0
    my_usdc: float = 0.0
    status: TradeStatus = TradeStatus.OPEN
    order_id: str = ""
    reason: str = ""              # why it was skipped/failed, if applicable
    opened_ts: int = 0
    closed_ts: Optional[int] = None
    market_end_ts: Optional[int] = None
    realized_pnl: float = 0.0
    current_price: float = 0.0
    db_id: Optional[int] = None

    # Live-only annotations (never persisted) --------------------------------
    # Where the current mark came from, surfaced in the UI so the user can see
    # at a glance that a price is a live streamed book price and not a stale
    # REST value: "stream" | "rest" | "last" | "resolved".
    price_source: str = "last"
    price_ts: float = 0.0
    # Set when an exchange reconciliation finds our recorded size differs from
    # the size actually held on-chain.
    sync_note: str = ""

    @property
    def unrealized_pnl(self) -> float:
        if self.status != TradeStatus.OPEN or not self.my_size:
            return 0.0
        return (self.current_price - self.my_price) * self.my_size

    @property
    def market_value(self) -> float:
        return (self.current_price or self.my_price) * self.my_size

    def to_dict(self) -> dict:
        return {
            "id": self.db_id,
            "targetTradeId": self.target_trade_id,
            "marketId": self.market_id,
            "tokenId": self.token_id,
            "outcome": self.outcome,
            "question": self.market_question,
            "side": self.side.value,
            "mode": self.mode,
            "targetPrice": self.target_price,
            "targetUsdc": self.target_usdc,
            "entryPrice": self.my_price,
            "size": self.my_size,
            "cost": self.my_usdc,
            "status": self.status.value,
            "orderId": self.order_id,
            "reason": self.reason,
            "openedTs": self.opened_ts,
            "closedTs": self.closed_ts,
            "marketEndTs": self.market_end_ts,
            "realizedPnl": self.realized_pnl,
            "currentPrice": self.current_price,
            "marketValue": self.market_value,
            "unrealizedPnl": self.unrealized_pnl,
            "priceSource": self.price_source,
            "priceTs": self.price_ts,
            "syncNote": self.sync_note,
        }


@dataclass
class OrderRecord:
    """One order submitted to the exchange, tracked through its whole life."""

    order_id: str
    token_id: str
    market_id: str
    market_question: str
    outcome: str
    side: Side
    price: float
    size: float
    filled_size: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    error: str = ""
    trade_db_id: Optional[int] = None
    created_ts: int = 0
    updated_ts: int = 0
    db_id: Optional[int] = None

    @property
    def is_pending(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.LIVE,
                               OrderStatus.MATCHED)

    def to_dict(self) -> dict:
        return {
            "id": self.db_id,
            "orderId": self.order_id,
            "tokenId": self.token_id,
            "marketId": self.market_id,
            "question": self.market_question,
            "outcome": self.outcome,
            "side": self.side.value,
            "price": self.price,
            "size": self.size,
            "filledSize": self.filled_size,
            "remaining": max(0.0, self.size - self.filled_size),
            "status": self.status.value,
            "error": self.error,
            "tradeId": self.trade_db_id,
            "createdTs": self.created_ts,
            "updatedTs": self.updated_ts,
            "pending": self.is_pending,
        }


@dataclass
class AccountSnapshot:
    """A point-in-time view of your account used by the dashboard."""

    connected_address: str = ""
    usdc_balance: float = 0.0        # free cash
    total_position_value: float = 0.0
    buying_power: float = 0.0
    connected: bool = False
    paper: bool = False
    account_label: str = ""
    updated_ts: float = 0.0

    @property
    def equity(self) -> float:
        return self.usdc_balance + self.total_position_value

    def to_dict(self) -> dict:
        return {
            "address": self.connected_address,
            "balance": self.usdc_balance,
            "positionValue": self.total_position_value,
            "buyingPower": self.buying_power,
            "equity": self.equity,
            "connected": self.connected,
            "paper": self.paper,
            "accountLabel": self.account_label,
            "updatedTs": self.updated_ts or time.time(),
        }
