"""
Strategy base class.

A strategy emits Signals based on market state. The engine takes those
signals, runs them through risk, sizes them, and turns them into Orders
on the appropriate exchange.

Strategies should be pure-ish: given the same market state, they emit
the same signals. State that influences signals (positions held, recent
fills, cooldowns) is passed in via StrategyContext, not stored on the
strategy itself. This makes them backtestable.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog

from core.models import Position, Signal, StrategyMode

log = structlog.get_logger(__name__)


@dataclass
class StrategyContext:
    """Snapshot of state passed to a strategy on each tick."""
    now: datetime
    open_positions: list[Position] = field(default_factory=list)
    available_balance_usd: Decimal = Decimal(0)
    extras: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    """Base class. Subclasses implement `tick`."""

    name: str = "unnamed"
    mode: StrategyMode = StrategyMode.DISABLED

    def __init__(self, *, mode: StrategyMode = StrategyMode.DISABLED, **params):
        self.mode = mode
        self.params = params
        self.log = log.bind(strategy=self.name)

    @abstractmethod
    async def tick(self, ctx: StrategyContext) -> list[Signal]:
        """Called periodically. Return signals to act on (may be empty)."""

    @property
    def enabled(self) -> bool:
        return self.mode != StrategyMode.DISABLED

    def __repr__(self) -> str:
        return f"<Strategy {self.name} mode={self.mode}>"
