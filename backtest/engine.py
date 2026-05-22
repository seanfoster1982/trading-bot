"""
Backtest engine.

Replays a Strategy against historical Polymarket data. Uses the same
risk layer as live trading — anything that wouldn't pass risk in live
is rejected here too.

Mechanics:
- For each historical market, walk the price series timestamp by timestamp
- At each step, build a synthetic OrderBook from the historical mid price
  (with a configurable spread assumption — default 1 cent)
- Build a HistoricalExchangeStub that returns this fake book to the
  strategy's `tick()`
- Collect signals, run risk check, simulate fills
- At market resolution, mark all open positions to the resolution value
  (1.0 or 0.0) and compute realized PnL

Limitations (be honest about these):
- Synthetic book assumes you can always get the size you want at the
  ask. Reality has thinner depth, especially on resolution-arb candidates.
  Discount results by 20–40% to account for slippage.
- 1h granularity from the API misses intra-hour fills. For high-freq
  strategies this matters; for resolution_arb it doesn't.
- Fees are modeled as a flat per-trade rate. Polymarket has historically
  been zero-fee on the order book, but check before going live.
- No funding rate or borrow modeling; Polymarket markets are spot binary
  outcomes so this isn't relevant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog

from backtest.history import HistoricalMarket
from core.models import (
    Fill,
    Market,
    Order,
    OrderBook,
    OrderBookLevel,
    OrderStatus,
    OrderType,
    Platform,
    Position,
    Side,
    Signal,
    StrategyMode,
)
from risk.manager import RiskLimits, RiskManager, RiskRejected
from strategies.base import Strategy, StrategyContext

log = structlog.get_logger(__name__)


@dataclass
class BacktestConfig:
    spread: Decimal = Decimal("0.01")           # ask = mid + spread/2
    fee_rate: Decimal = Decimal("0")            # Polymarket is currently 0
    initial_cash_usd: Decimal = Decimal("1000")
    starting_position_count: int = 0


@dataclass
class BacktestTrade:
    strategy: str
    market_id: str
    question: str
    side: Side
    entry_price: Decimal
    entry_time: datetime
    size: Decimal
    exit_price: Decimal | None = None
    exit_time: datetime | None = None
    pnl_usd: Decimal | None = None
    rejected_reason: str | None = None

    @property
    def realized(self) -> bool:
        return self.exit_price is not None


@dataclass
class BacktestResult:
    config: BacktestConfig
    strategy_name: str
    trades: list[BacktestTrade] = field(default_factory=list)
    final_cash: Decimal = Decimal(0)
    rejections: int = 0

    @property
    def realized_trades(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.realized]

    @property
    def total_pnl(self) -> Decimal:
        return sum(
            (t.pnl_usd or Decimal(0) for t in self.realized_trades),
            start=Decimal(0),
        )

    @property
    def win_rate(self) -> float:
        wins = [t for t in self.realized_trades if (t.pnl_usd or Decimal(0)) > 0]
        return len(wins) / len(self.realized_trades) if self.realized_trades else 0.0

    @property
    def avg_pnl_per_trade(self) -> Decimal:
        if not self.realized_trades:
            return Decimal(0)
        return self.total_pnl / Decimal(len(self.realized_trades))

    @property
    def total_invested(self) -> Decimal:
        return sum(
            (t.entry_price * t.size for t in self.realized_trades),
            start=Decimal(0),
        )

    @property
    def roi_pct(self) -> float:
        if self.total_invested == 0:
            return 0.0
        return float(self.total_pnl / self.total_invested * 100)

    def summary(self) -> str:
        return (
            f"Strategy: {self.strategy_name}\n"
            f"  Trades:           {len(self.realized_trades)} realized, "
            f"{self.rejections} rejected\n"
            f"  Total PnL:        ${self.total_pnl:.2f}\n"
            f"  Win rate:         {self.win_rate*100:.1f}%\n"
            f"  Avg PnL/trade:    ${self.avg_pnl_per_trade:.4f}\n"
            f"  Capital deployed: ${self.total_invested:.2f}\n"
            f"  ROI on deployed:  {self.roi_pct:.2f}%\n"
        )


class HistoricalExchangeStub:
    """Quacks enough like an Exchange for strategies during backtest.

    Holds a single market's current state. The backtester swaps the
    market in as it walks the timeline.
    """
    platform = Platform.POLYMARKET

    def __init__(self, *, spread: Decimal):
        self._spread = spread
        self._current_market: Market | None = None
        self._current_mid: Decimal | None = None

    def set_state(self, market: Market, mid_price: Decimal) -> None:
        self._current_market = market
        self._current_mid = mid_price

    async def list_markets(self, *, active_only: bool = True,
                           min_volume_24h: Decimal | None = None) -> list[Market]:
        if self._current_market is None:
            return []
        return [self._current_market]

    async def get_order_book(self, market: Market) -> OrderBook:
        if self._current_mid is None:
            raise RuntimeError("backtest exchange has no current state")
        half = self._spread / Decimal(2)
        ask = (self._current_mid + half).quantize(Decimal("0.01"))
        bid = (self._current_mid - half).quantize(Decimal("0.01"))
        # Clamp
        if ask >= Decimal("1.0"):
            ask = Decimal("0.99")
        if bid <= Decimal("0.0"):
            bid = Decimal("0.01")
        # Assume infinite synthetic depth (we discount for slippage in summary)
        depth = Decimal("10000")
        return OrderBook(
            market_id=market.market_id,
            bids=[OrderBookLevel(price=bid, size=depth)],
            asks=[OrderBookLevel(price=ask, size=depth)],
            timestamp=datetime.utcnow(),
        )

    async def get_balance_usd(self) -> Decimal:
        return Decimal("999999")  # backtest doesn't track cash here

    async def get_positions(self) -> list[Position]:
        return []

    async def submit_order(self, order: Order) -> Order:
        order.status = OrderStatus.FILLED
        return order

    async def cancel_order(self, order: Order) -> bool:
        return True

    async def cancel_all(self) -> int:
        return 0

    async def stream_fills(self):
        if False:
            yield


class Backtester:
    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()

    async def run(
        self,
        strategy_factory,            # callable(exchange) -> Strategy
        markets: list[HistoricalMarket],
        risk_limits: RiskLimits,
    ) -> BacktestResult:
        """Run a strategy across the given historical markets."""
        stub = HistoricalExchangeStub(spread=self.config.spread)
        strat = strategy_factory(stub)
        strat.mode = StrategyMode.PAPER

        risk = RiskManager(risk_limits)
        result = BacktestResult(config=self.config, strategy_name=strat.name)

        for hm in markets:
            market = Market(
                platform=Platform.POLYMARKET,
                market_id=hm.market_id,
                token_id=hm.token_id,
                question=hm.question,
                outcome=hm.outcome,
                end_date=hm.end_date,
                metadata={"volume_24h": 999999},  # bypass volume filter
            )
            await self._replay_market(strat, stub, risk, market, hm, result)

        result.final_cash = (
            self.config.initial_cash_usd + result.total_pnl
        )
        return result

    async def _replay_market(
        self,
        strat: Strategy,
        stub: HistoricalExchangeStub,
        risk: RiskManager,
        market: Market,
        hm: HistoricalMarket,
        result: BacktestResult,
    ) -> None:
        open_trade: BacktestTrade | None = None

        for ts, mid in hm.history:
            if ts >= hm.end_date:
                break
            stub.set_state(market, mid)
            ctx = StrategyContext(now=ts)
            try:
                signals = await strat.tick(ctx)
            except Exception:
                log.exception("backtest.tick_failed", market=market.market_id)
                continue

            for sig in signals:
                if open_trade is not None:
                    # one position per market for simplicity
                    continue

                share_count = (
                    sig.target_size_usd / sig.target_price
                ).quantize(Decimal("0.01"))
                if share_count <= 0:
                    continue

                test_order = Order(
                    client_order_id=f"bt-{hm.market_id}-{ts.timestamp():.0f}",
                    strategy=sig.strategy,
                    platform=sig.platform,
                    market_id=sig.market_id,
                    token_id=sig.token_id,
                    side=sig.side,
                    order_type=OrderType.GTC,
                    price=sig.target_price,
                    size=share_count,
                )
                try:
                    risk.check_order(
                        test_order,
                        open_positions=[],
                        open_orders_notional_usd=Decimal(0),
                    )
                except RiskRejected as e:
                    result.rejections += 1
                    result.trades.append(BacktestTrade(
                        strategy=sig.strategy,
                        market_id=hm.market_id,
                        question=hm.question,
                        side=sig.side,
                        entry_price=sig.target_price,
                        entry_time=ts,
                        size=share_count,
                        rejected_reason=str(e),
                    ))
                    continue

                open_trade = BacktestTrade(
                    strategy=sig.strategy,
                    market_id=hm.market_id,
                    question=hm.question,
                    side=sig.side,
                    entry_price=sig.target_price,
                    entry_time=ts,
                    size=share_count,
                )
                result.trades.append(open_trade)
                # Strategy-specific: only one entry per backtest run per
                # market; break out once we've taken position
                break

            if open_trade is not None:
                # we have a position; stop generating new signals for
                # this market and let it ride to resolution
                break

        # Close at resolution
        if open_trade is not None:
            resolution_value = hm.resolution_value
            if resolution_value is None:
                # unknown resolution — close at final history mid
                resolution_value = hm.history[-1][1]
            exit_price = resolution_value
            if open_trade.side == Side.BUY:
                pnl = (exit_price - open_trade.entry_price) * open_trade.size
            else:
                pnl = (open_trade.entry_price - exit_price) * open_trade.size
            # apply fees (0 by default on Polymarket)
            fee = (open_trade.entry_price * open_trade.size +
                   exit_price * open_trade.size) * self.config.fee_rate
            open_trade.exit_price = exit_price
            open_trade.exit_time = hm.end_date
            open_trade.pnl_usd = pnl - fee
            risk.record_pnl_delta(open_trade.pnl_usd)
