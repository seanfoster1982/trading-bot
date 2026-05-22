"""
Domain models. These types cross strategy/exchange boundaries.

Designed to be exchange-agnostic where possible. Polymarket-specific fields
sit in metadata; same for Solana. A strategy emits Signals, the engine turns
them into Orders, the exchange returns Fills.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Platform(str, Enum):
    POLYMARKET = "polymarket"
    SOLANA = "solana"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    GTC = "GTC"   # good till cancel — resting limit
    FOK = "FOK"   # fill or kill — immediate full fill or cancel
    FAK = "FAK"   # fill and kill — partial OK, cancel rest
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


class StrategyMode(str, Enum):
    DISABLED = "disabled"
    PAPER = "paper"
    LIVE = "live"


class Market(BaseModel):
    """A tradeable market. For Polymarket, one outcome of a binary question."""
    platform: Platform
    market_id: str                # platform-native ID
    token_id: str | None = None   # Polymarket: outcome token; Solana: mint
    question: str | None = None   # Polymarket: human-readable question
    outcome: str | None = None    # Polymarket: "Yes" / "No"
    end_date: datetime | None = None
    tick_size: Decimal = Decimal("0.01")
    min_size: Decimal = Decimal("5")
    metadata: dict[str, Any] = Field(default_factory=dict)


class OrderBookLevel(BaseModel):
    price: Decimal
    size: Decimal


class OrderBook(BaseModel):
    market_id: str
    bids: list[OrderBookLevel]      # sorted high to low
    asks: list[OrderBookLevel]      # sorted low to high
    timestamp: datetime

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def midpoint(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / Decimal(2)

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


class Signal(BaseModel):
    """A strategy's intent to trade. Risk layer turns this into an Order."""
    strategy: str
    platform: Platform
    market_id: str
    token_id: str | None = None
    side: Side
    target_price: Decimal           # limit price
    target_size_usd: Decimal        # USD value to deploy
    confidence: float = 0.5         # 0..1, used for sizing
    reason: str = ""                # human-readable signal rationale
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Order(BaseModel):
    id: str | None = None           # platform order ID, set after submission
    client_order_id: str            # our UUID
    strategy: str
    platform: Platform
    market_id: str
    token_id: str | None = None
    side: Side
    order_type: OrderType
    price: Decimal
    size: Decimal                   # in base units (shares for Polymarket)
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    is_paper: bool = True
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Fill(BaseModel):
    order_client_id: str
    platform: Platform
    market_id: str
    side: Side
    price: Decimal
    size: Decimal
    fee: Decimal = Decimal(0)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class Position(BaseModel):
    platform: Platform
    market_id: str
    token_id: str | None = None
    size: Decimal                   # signed: positive=long, negative=short
    avg_entry: Decimal
    realized_pnl: Decimal = Decimal(0)
    unrealized_pnl: Decimal = Decimal(0)
    strategy: str | None = None
    opened_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def notional_usd(self) -> Decimal:
        return abs(self.size) * self.avg_entry
