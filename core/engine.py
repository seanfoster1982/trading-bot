"""
Engine. The orchestrator.

Each tick:
1. Build a StrategyContext snapshot (positions, balance, time)
2. For each enabled strategy, await tick(), collect signals
3. For each signal: convert to Order, check risk, submit (or paper-fill)
4. Persist orders + fills to DB (when persistence is wired)
5. Sleep until next tick

Strategies run in parallel via asyncio.gather. The risk layer is the
single chokepoint — strategies cannot bypass it.

In paper mode, orders never hit the exchange. We simulate fills assuming
the limit price is achievable if the book supports it. This is optimistic
on slippage and pessimistic on time-in-force; reality lies in between.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from decimal import Decimal

import structlog

from core.models import (
    Order,
    OrderStatus,
    OrderType,
    Platform,
    Side,
    Signal,
    StrategyMode,
)
from exchanges.base import Exchange
from risk.manager import RiskManager, RiskRejected
from strategies.base import Strategy, StrategyContext

log = structlog.get_logger(__name__)


class Engine:
    def __init__(
        self,
        *,
        exchanges: dict[Platform, Exchange],
        strategies: list[Strategy],
        risk: RiskManager,
        tick_seconds: float = 30.0,
        live_global: bool = False,
        persistence=None,            # data.persistence.Persistence, optional
        equity_log_every_ticks: int = 10,
    ):
        self.exchanges = exchanges
        self.strategies = strategies
        self.risk = risk
        self.tick_seconds = tick_seconds
        self.live_global = live_global
        self.persistence = persistence
        self.equity_log_every = equity_log_every_ticks
        self._stop = asyncio.Event()
        self._paper_orders: list[Order] = []
        self._tick_count = 0

    async def run(self) -> None:
        log.info("engine.start", strategies=[s.name for s in self.strategies],
                 live_global=self.live_global, tick_seconds=self.tick_seconds,
                 persistence=bool(self.persistence))
        try:
            while not self._stop.is_set():
                tick_start = datetime.utcnow()
                try:
                    await self._tick(tick_start)
                except Exception:
                    log.exception("engine.tick_failed")
                self._tick_count += 1
                # sleep, but wake up early if stop is set
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            log.info("engine.stopped")

    def stop(self) -> None:
        self._stop.set()

    async def _tick(self, now: datetime) -> None:
        ctx = StrategyContext(now=now)

        # Run all strategies in parallel
        results = await asyncio.gather(
            *[s.tick(ctx) for s in self.strategies if s.enabled],
            return_exceptions=True,
        )
        all_signals: list[tuple[Strategy, Signal]] = []
        for strat, res in zip([s for s in self.strategies if s.enabled], results):
            if isinstance(res, Exception):
                log.exception("engine.strategy_failed", strategy=strat.name, exc=str(res))
                continue
            for sig in res:
                all_signals.append((strat, sig))

        log.info("engine.tick", signal_count=len(all_signals))

        # Sequentially process signals — risk check is stateful
        for strat, sig in all_signals:
            try:
                order = self._signal_to_order(strat, sig)
            except Exception:
                log.exception("engine.signal_to_order_failed", signal=sig)
                continue

            try:
                self.risk.check_order(
                    order,
                    open_positions=ctx.open_positions,
                    open_orders_notional_usd=Decimal(0),  # TODO: track
                )
            except RiskRejected as e:
                log.warning("engine.risk_rejected", reason=str(e),
                            strategy=strat.name, market=order.market_id)
                # Persist the rejected order so the dashboard sees it
                if self.persistence is not None:
                    order.status = OrderStatus.REJECTED
                    order.error = f"risk: {e}"
                    await asyncio.to_thread(self.persistence.record_order, order)
                continue

            await self._execute(order, strat)

        # Periodically snapshot equity from paper trades
        if (
            self.persistence is not None
            and self.equity_log_every > 0
            and self._tick_count % self.equity_log_every == 0
        ):
            await asyncio.to_thread(self._snapshot_equity)

    def _signal_to_order(self, strat: Strategy, signal: Signal) -> Order:
        """Convert a Signal into a concrete Order with sizing applied."""
        share_count = signal.target_size_usd / signal.target_price
        # Quantize to the platform's tick — Polymarket is 0.01 shares typically
        share_count = share_count.quantize(Decimal("0.01"))
        if share_count <= 0:
            raise ValueError(f"non-positive share count: {share_count}")

        is_paper = strat.mode != StrategyMode.LIVE or not self.live_global

        return Order(
            client_order_id=f"{strat.name[:12]}-{uuid.uuid4().hex[:8]}",
            strategy=strat.name,
            platform=signal.platform,
            market_id=signal.market_id,
            token_id=signal.token_id,
            side=signal.side,
            order_type=OrderType.GTC,
            price=signal.target_price,
            size=share_count,
            is_paper=is_paper,
            metadata={"signal_reason": signal.reason, **signal.metadata},
        )

    async def _execute(self, order: Order, strat: Strategy) -> None:
        if order.is_paper:
            order.status = OrderStatus.FILLED
            order.id = f"paper-{uuid.uuid4().hex[:12]}"
            self._paper_orders.append(order)
            log.info("engine.paper_order",
                     strategy=strat.name,
                     market=order.market_id,
                     side=order.side,
                     price=str(order.price),
                     size=str(order.size),
                     reason=order.metadata.get("signal_reason"))
            if self.persistence is not None:
                await asyncio.to_thread(self.persistence.record_paper_fill, order)
            return

        exchange = self.exchanges.get(order.platform)
        if exchange is None:
            log.error("engine.no_exchange", platform=order.platform)
            order.status = OrderStatus.REJECTED
            order.error = f"no exchange registered for {order.platform}"
            if self.persistence is not None:
                await asyncio.to_thread(self.persistence.record_order, order)
            return

        await exchange.submit_order(order)
        log.info("engine.live_order",
                 strategy=strat.name,
                 market=order.market_id,
                 status=order.status,
                 platform_id=order.id,
                 error=order.error)
        if self.persistence is not None:
            await asyncio.to_thread(self.persistence.record_order, order)

    def _snapshot_equity(self) -> None:
        """Compute and persist a single equity-curve point.
        Cash is risk's daily PnL accumulator (paper). Positions are mark-
        to-last-fill (we don't have live mid prices in this scaffold)."""
        if self.persistence is None:
            return
        positions = self.persistence.list_open_positions()
        positions_value = sum(
            ((p.size or Decimal(0)) * (p.avg_entry or Decimal(0)) for p in positions),
            start=Decimal(0),
        )
        # cash_usd = starting balance + daily realized — close enough as a
        # paper proxy. Real impl would track cash separately.
        cash = self.risk.state.daily_pnl_usd
        total = cash + positions_value
        self.persistence.record_equity_point(
            cash_usd=cash, positions_usd=positions_value, total_equity_usd=total,
        )

    @property
    def paper_orders(self) -> list[Order]:
        return list(self._paper_orders)
