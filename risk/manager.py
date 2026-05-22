"""
Risk layer. Every order flows through RiskManager.check_order() before
submission. There is no path that bypasses this.

Hard limits:
- Per-market notional cap
- Total open exposure cap
- Daily loss cap (auto-disables bot when breached)
- Max number of open positions
- Kill switch: a single flag that vetoes every order

Strategy-specific limits (max positions per strategy, etc.) layer on top
via StrategyConfig, but cannot loosen the global limits — only tighten them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import structlog

from core.models import Order, Platform, Position

log = structlog.get_logger(__name__)


@dataclass
class RiskLimits:
    max_position_usd: Decimal
    max_total_exposure_usd: Decimal
    max_daily_loss_usd: Decimal
    max_open_positions: int
    kill_on_daily_loss: bool = True


@dataclass
class RiskState:
    """Mutable runtime state. Reset daily by the scheduler."""
    daily_pnl_usd: Decimal = Decimal(0)
    daily_pnl_date: date = field(default_factory=date.today)
    killed: bool = False
    kill_reason: str | None = None


class RiskRejected(Exception):
    """Raised when the risk layer vetoes an order. Always logged."""


class RiskManager:
    def __init__(self, limits: RiskLimits):
        self.limits = limits
        self.state = RiskState()

    # ----- main check ------------------------------------------------------

    def check_order(
        self,
        order: Order,
        *,
        open_positions: list[Position],
        open_orders_notional_usd: Decimal,
    ) -> None:
        """Raise RiskRejected if the order should not be submitted."""
        self._maybe_roll_daily()

        if self.state.killed:
            raise RiskRejected(f"kill switch active: {self.state.kill_reason}")

        notional = order.price * order.size
        if notional > self.limits.max_position_usd:
            raise RiskRejected(
                f"order notional ${notional:.2f} > max_position_usd "
                f"${self.limits.max_position_usd}"
            )

        existing_notional = sum(
            (p.notional_usd for p in open_positions), start=Decimal(0)
        )
        projected_total = existing_notional + open_orders_notional_usd + notional
        if projected_total > self.limits.max_total_exposure_usd:
            raise RiskRejected(
                f"projected exposure ${projected_total:.2f} > max_total_exposure_usd "
                f"${self.limits.max_total_exposure_usd}"
            )

        if len(open_positions) >= self.limits.max_open_positions:
            # Allow if this order is reducing an existing position
            same_market = any(
                p.platform == order.platform and p.market_id == order.market_id
                for p in open_positions
            )
            if not same_market:
                raise RiskRejected(
                    f"open positions {len(open_positions)} >= "
                    f"max_open_positions {self.limits.max_open_positions}"
                )

        if self.limits.kill_on_daily_loss and (
            self.state.daily_pnl_usd <= -self.limits.max_daily_loss_usd
        ):
            self.kill(f"daily loss limit hit: ${self.state.daily_pnl_usd:.2f}")
            raise RiskRejected("kill switch tripped on daily loss")

    # ----- pnl tracking ----------------------------------------------------

    def record_pnl_delta(self, pnl_delta_usd: Decimal) -> None:
        self._maybe_roll_daily()
        self.state.daily_pnl_usd += pnl_delta_usd
        log.info("risk.pnl_delta", delta=str(pnl_delta_usd),
                 daily=str(self.state.daily_pnl_usd))
        if (
            self.limits.kill_on_daily_loss
            and self.state.daily_pnl_usd <= -self.limits.max_daily_loss_usd
        ):
            self.kill(f"daily loss limit hit: ${self.state.daily_pnl_usd:.2f}")

    def _maybe_roll_daily(self) -> None:
        today = date.today()
        if today != self.state.daily_pnl_date:
            log.info("risk.daily_roll", from_date=str(self.state.daily_pnl_date),
                     to_date=str(today), final_pnl=str(self.state.daily_pnl_usd))
            self.state.daily_pnl_usd = Decimal(0)
            self.state.daily_pnl_date = today

    # ----- kill switch -----------------------------------------------------

    def kill(self, reason: str) -> None:
        if not self.state.killed:
            log.critical("risk.kill_switch_engaged", reason=reason,
                         at=datetime.utcnow().isoformat())
        self.state.killed = True
        self.state.kill_reason = reason

    def revive(self) -> None:
        """Manual revive only. Should require human action."""
        log.warning("risk.kill_switch_revived",
                    previous_reason=self.state.kill_reason)
        self.state.killed = False
        self.state.kill_reason = None
