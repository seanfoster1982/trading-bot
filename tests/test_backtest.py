"""
Backtest engine tests. The backtester drives every strategy decision,
so its math must be right or every backtest result is meaningless.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.engine import Backtester, BacktestConfig, HistoricalExchangeStub
from backtest.history import HistoricalMarket
from core.models import Platform, Side, Signal, StrategyMode
from risk.manager import RiskLimits
from strategies.base import Strategy, StrategyContext


# ----- fixtures ------------------------------------------------------------

def make_history(prices: list[float], end: datetime) -> list[tuple]:
    """Build N-hour history ending at `end`."""
    out = []
    for i, p in enumerate(prices):
        ts = end - timedelta(hours=len(prices) - i)
        out.append((ts.replace(tzinfo=timezone.utc), Decimal(str(p))))
    return out


def make_market(
    *,
    market_id: str = "m1",
    prices: list[float] | None = None,
    resolved_to: int | None = 1,
    hours_until_end: int = 1,
) -> HistoricalMarket:
    end = datetime.now(timezone.utc) - timedelta(hours=hours_until_end)
    prices = prices or [0.96, 0.97, 0.97, 0.98]
    return HistoricalMarket(
        market_id=market_id,
        token_id=f"tok-{market_id}",
        question=f"Question {market_id}",
        outcome="YES",
        resolved_to=resolved_to,
        end_date=end,
        history=make_history(prices, end),
    )


class AlwaysBuy(Strategy):
    """Test strategy: emit a buy at the current ask, once per market."""
    name = "test_buy"

    def __init__(self, exchange):
        super().__init__(mode=StrategyMode.PAPER)
        self.exchange = exchange
        self._fired: set[str] = set()

    async def tick(self, ctx: StrategyContext) -> list[Signal]:
        markets = await self.exchange.list_markets()
        if not markets:
            return []
        m = markets[0]
        if m.market_id in self._fired:
            return []
        self._fired.add(m.market_id)
        book = await self.exchange.get_order_book(m)
        if book.best_ask is None:
            return []
        return [Signal(
            strategy=self.name,
            platform=Platform.POLYMARKET,
            market_id=m.market_id,
            token_id=m.token_id,
            side=Side.BUY,
            target_price=book.best_ask,
            target_size_usd=Decimal("10"),
        )]


# ----- tests --------------------------------------------------------------

@pytest.mark.asyncio
async def test_backtest_winning_buy_resolves_to_one():
    """Buy at ask, resolves YES (price → 1.0). PnL = (1.0 - entry) * size."""
    bt = Backtester(BacktestConfig(spread=Decimal("0.01")))
    market = make_market(prices=[0.96, 0.96, 0.96], resolved_to=1)
    result = await bt.run(
        lambda ex: AlwaysBuy(ex),
        [market],
        RiskLimits(
            max_position_usd=Decimal("100"),
            max_total_exposure_usd=Decimal("1000"),
            max_daily_loss_usd=Decimal("1000000"),
            max_open_positions=99,
            kill_on_daily_loss=False,
        ),
    )
    assert len(result.realized_trades) == 1
    t = result.realized_trades[0]
    assert t.side == Side.BUY
    # mid 0.96 + spread/2 (0.005) = 0.965, quantized to 0.96 via banker's rounding
    assert t.entry_price == Decimal("0.96")
    assert t.exit_price == Decimal("1")
    # size = (10 / 0.96) quantized to 0.01 = 10.42
    assert t.size == Decimal("10.42")
    expected_pnl = (Decimal("1") - Decimal("0.96")) * Decimal("10.42")
    assert t.pnl_usd == expected_pnl


@pytest.mark.asyncio
async def test_backtest_losing_buy_resolves_to_zero():
    """Buy at high price, resolves NO. Lose entry_price * size."""
    bt = Backtester(BacktestConfig(spread=Decimal("0.01")))
    market = make_market(prices=[0.96], resolved_to=0)
    result = await bt.run(
        lambda ex: AlwaysBuy(ex),
        [market],
        RiskLimits(
            max_position_usd=Decimal("100"),
            max_total_exposure_usd=Decimal("1000"),
            max_daily_loss_usd=Decimal("1000000"),
            max_open_positions=99,
            kill_on_daily_loss=False,
        ),
    )
    assert len(result.realized_trades) == 1
    t = result.realized_trades[0]
    assert t.exit_price == Decimal("0")
    expected_pnl = -t.entry_price * t.size
    assert t.pnl_usd == expected_pnl


@pytest.mark.asyncio
async def test_backtest_risk_layer_rejects():
    """Position size cap should reject all trades and record rejections."""
    bt = Backtester(BacktestConfig())
    market = make_market(prices=[0.96])
    result = await bt.run(
        lambda ex: AlwaysBuy(ex),
        [market],
        RiskLimits(
            max_position_usd=Decimal("0.01"),  # too small for any trade
            max_total_exposure_usd=Decimal("1000"),
            max_daily_loss_usd=Decimal("1000000"),
            max_open_positions=99,
            kill_on_daily_loss=False,
        ),
    )
    assert len(result.realized_trades) == 0
    assert result.rejections >= 1


@pytest.mark.asyncio
async def test_backtest_aggregates_pnl_across_markets():
    bt = Backtester(BacktestConfig())
    markets = [
        make_market(market_id="winner", prices=[0.96], resolved_to=1),
        make_market(market_id="loser",  prices=[0.96], resolved_to=0),
    ]
    result = await bt.run(
        lambda ex: AlwaysBuy(ex),
        markets,
        RiskLimits(
            max_position_usd=Decimal("100"),
            max_total_exposure_usd=Decimal("1000"),
            max_daily_loss_usd=Decimal("1000000"),
            max_open_positions=99,
            kill_on_daily_loss=False,
        ),
    )
    assert len(result.realized_trades) == 2
    winner = next(t for t in result.realized_trades if t.market_id == "winner")
    loser = next(t for t in result.realized_trades if t.market_id == "loser")
    assert winner.pnl_usd > 0
    assert loser.pnl_usd < 0
    assert result.total_pnl == winner.pnl_usd + loser.pnl_usd


@pytest.mark.asyncio
async def test_historical_exchange_stub_clamps_prices_at_extremes():
    """When mid price is near 1.0, ask should clamp at 0.99."""
    stub = HistoricalExchangeStub(spread=Decimal("0.01"))
    from core.models import Market
    m = Market(platform=Platform.POLYMARKET, market_id="x", token_id="t",
               metadata={"volume_24h": 0})
    stub.set_state(m, Decimal("0.999"))
    book = await stub.get_order_book(m)
    assert book.best_ask <= Decimal("0.99")
    assert book.best_bid > Decimal("0")
