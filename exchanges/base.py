"""
Exchange abstraction. Every adapter (Polymarket, Solana/Jupiter) implements
this protocol. Strategies depend on this interface, never on a specific
exchange — keeps strategies portable and testable.

Async by default. Methods raise on hard errors; transient errors are caught
and retried inside the adapter using tenacity.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from core.models import Fill, Market, Order, OrderBook, Platform, Position


class Exchange(ABC):
    """Read + write interface to a single trading venue."""

    platform: Platform

    # ----- read ------------------------------------------------------------

    @abstractmethod
    async def list_markets(
        self,
        *,
        active_only: bool = True,
        min_volume_24h: Decimal | None = None,
    ) -> list[Market]:
        """All tradeable markets, optionally filtered."""

    @abstractmethod
    async def get_order_book(self, market: Market) -> OrderBook:
        """Top-N depth for a market."""

    @abstractmethod
    async def get_balance_usd(self) -> Decimal:
        """Available USD/USDC balance for trading."""

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Open positions held on this venue."""

    # ----- write -----------------------------------------------------------

    @abstractmethod
    async def submit_order(self, order: Order) -> Order:
        """Submit, return order with platform ID and updated status."""

    @abstractmethod
    async def cancel_order(self, order: Order) -> bool:
        """Cancel; returns True if accepted by venue."""

    @abstractmethod
    async def cancel_all(self) -> int:
        """Cancel everything open. Returns count cancelled. Used by kill switch."""

    # ----- streaming -------------------------------------------------------

    @abstractmethod
    async def stream_fills(self):
        """Async generator yielding Fill objects for our orders."""
