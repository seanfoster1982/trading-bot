"""
Tests for the score_based benchmark strategy.

This is a baseline, not a real strategy. Tests focus on:
- Score is bounded [0, 1] for all reasonable inputs
- Score is monotonic in the directions you'd expect (more liquidity → higher)
- Strategy respects spread filter
- Strategy respects threshold
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from core.models import (
    Market,
    OrderBook,
    OrderBookLevel,
    Platform,
    Side,
    StrategyMode,
)
from strategies.base import StrategyContext
from strategies.polymarket.score_based import ScoreBasedStrategy, score_market


def test_score_bounded_zero_inputs():
    s = score_market(volume_24h=0, liquidity=0, spread=1.0, midpoint=0.0)
    assert 0.0 <= s <= 1.0


def test_score_bounded_huge_inputs():
    s = score_market(volume_24h=10**9, liquidity=10**9, spread=0.001, midpoint=0.5)
    assert 0.0 <= s <= 1.0


def test_score_monotonic_in_liquidity():
    low = score_market(volume_24h=1000, liquidity=100, spread=0.01, midpoint=0.5)
    high = score_market(volume_24h=1000, liquidity=100_000, spread=0.01, midpoint=0.5)
    assert high > low


def test_score_monotonic_in_volume():
    low = score_market(volume_24h=100, liquidity=10_000, spread=0.01, midpoint=0.5)
    high = score_market(volume_24h=100_000, liquidity=10_000, spread=0.01, midpoint=0.5)
    assert high > low


def test_score_penalizes_wide_spread():
    tight = score_market(volume_24h=10_000, liquidity=10_000, spread=0.005, midpoint=0.5)
    wide = score_market(volume_24h=10_000, liquidity=10_000, spread=0.10, midpoint=0.5)
    assert tight > wide


def test_score_rewards_uncertainty():
    # near 0.5 should score higher on the extremity component
    uncertain = score_market(volume_24h=10_000, liquidity=10_000, spread=0.01, midpoint=0.5)
    near_certain = score_market(volume_24h=10_000, liquidity=10_000, spread=0.01, midpoint=0.95)
    assert uncertain > near_certain


def test_score_handles_none_inputs():
    # spread or midpoint can be None — should not raise
    s = score_market(volume_24h=1000, liquidity=1000, spread=None, midpoint=None)
    assert 0.0 <= s <= 1.0


# ----- strategy-level tests -----------------------------------------------

class StubExchange:
    """Minimal exchange for strategy unit tests."""
    platform = Platform.POLYMARKET

    def __init__(self, markets, books):
        self._markets = markets
        self._books = {m.market_id: b for m, b in zip(markets, books)}

    async def list_markets(self, *, active_only=True, min_volume_24h=None):
        return list(self._markets)

    async def get_order_book(self, market):
        return self._books[market.market_id]


def make_market(market_id: str, vol: float = 10_000, liq: float = 10_000) -> Market:
    return Market(
        platform=Platform.POLYMARKET,
        market_id=market_id,
        token_id=f"tok-{market_id}",
        question=f"Q{market_id}",
        outcome="YES",
        end_date=None,
        metadata={"volume_24h": vol, "liquidity": liq},
    )


def make_book(market_id: str, bid: str, ask: str) -> OrderBook:
    return OrderBook(
        market_id=market_id,
        bids=[OrderBookLevel(price=Decimal(bid), size=Decimal("100"))],
        asks=[OrderBookLevel(price=Decimal(ask), size=Decimal("100"))],
        timestamp=datetime.utcnow(),
    )


@pytest.mark.asyncio
async def test_score_based_filters_wide_spread():
    """A market with a wider-than-allowed spread should generate no signal."""
    m = make_market("wide", vol=50_000, liq=50_000)
    b = make_book("wide", bid="0.40", ask="0.60")  # 20¢ spread
    ex = StubExchange([m], [b])
    strat = ScoreBasedStrategy(
        exchange=ex,
        mode=StrategyMode.PAPER,
        score_threshold=0.0,            # very permissive
        max_spread=Decimal("0.05"),
    )
    ctx = StrategyContext(now=datetime.utcnow())
    sigs = await strat.tick(ctx)
    assert sigs == []


@pytest.mark.asyncio
async def test_score_based_emits_signal_above_threshold():
    m = make_market("good", vol=50_000, liq=50_000)
    b = make_book("good", bid="0.49", ask="0.51")
    ex = StubExchange([m], [b])
    strat = ScoreBasedStrategy(
        exchange=ex,
        mode=StrategyMode.PAPER,
        score_threshold=0.4,
        max_spread=Decimal("0.05"),
    )
    ctx = StrategyContext(now=datetime.utcnow())
    sigs = await strat.tick(ctx)
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY
    assert sigs[0].target_price == Decimal("0.51")
    assert "score_based" in sigs[0].reason


@pytest.mark.asyncio
async def test_score_based_disabled_emits_nothing():
    m = make_market("good", vol=50_000, liq=50_000)
    b = make_book("good", bid="0.49", ask="0.51")
    ex = StubExchange([m], [b])
    strat = ScoreBasedStrategy(
        exchange=ex,
        mode=StrategyMode.DISABLED,
        score_threshold=0.0,
    )
    ctx = StrategyContext(now=datetime.utcnow())
    assert await strat.tick(ctx) == []
