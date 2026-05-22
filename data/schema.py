"""
Persistence schema. SQLAlchemy 2.x style. Tables:

- markets:        cache of platform metadata
- orders:         every order we attempted (paper or live)
- fills:          executions, parent → orders
- positions:      current holdings derived from fills
- equity_points:  daily equity snapshot for performance tracking
- alerts:         significant events (kill switch, large losses, etc.)

Use Alembic for migrations once you go beyond local dev.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class MarketRow(Base):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    token_id: Mapped[str | None] = mapped_column(String(128))
    question: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(64))
    end_date: Mapped[datetime | None] = mapped_column(DateTime)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_markets_platform_market_id", "platform", "market_id", unique=True),
    )


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    platform_order_id: Mapped[str | None] = mapped_column(String(128), index=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    token_id: Mapped[str | None] = mapped_column(String(128))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16))
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    size: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    status: Mapped[str] = mapped_column(String(32))
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    fills: Mapped[list["FillRow"]] = relationship(back_populates="order")


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32))
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    side: Mapped[str] = mapped_column(String(8))
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    size: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    fee: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal(0))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    order: Mapped[OrderRow] = relationship(back_populates="fills")


class PositionRow(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    token_id: Mapped[str | None] = mapped_column(String(128))
    size: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    avg_entry: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal(0))
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal(0))
    strategy: Mapped[str | None] = mapped_column(String(64))
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)


class EquityPointRow(Base):
    __tablename__ = "equity_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    cash_usd: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    positions_usd: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    total_equity_usd: Mapped[Decimal] = mapped_column(Numeric(20, 8))


class AlertRow(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    severity: Mapped[str] = mapped_column(String(16))   # info, warn, error, critical
    source: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)
