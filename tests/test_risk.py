"""
Risk layer tests. The risk layer is the last line of defense; it has
the most thorough tests in the codebase.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from core.models import Order, OrderType, Platform, Position, Side
from risk.manager import RiskLimits, RiskManager, RiskRejected


def make_order(
    *,
    price: str = "0.5",
    size: str = "10",
    market_id: str = "M1",
    platform: Platform = Platform.POLYMARKET,
) -> Order:
    return Order(
        client_order_id="test-1",
        strategy="test",
        platform=platform,
        market_id=market_id,
        token_id="T1",
        side=Side.BUY,
        order_type=OrderType.GTC,
        price=Decimal(price),
        size=Decimal(size),
    )


def make_limits(**overrides) -> RiskLimits:
    base = dict(
        max_position_usd=Decimal("50"),
        max_total_exposure_usd=Decimal("500"),
        max_daily_loss_usd=Decimal("100"),
        max_open_positions=5,
        kill_on_daily_loss=True,
    )
    base.update(overrides)
    return RiskLimits(**base)


def test_passes_within_limits():
    rm = RiskManager(make_limits())
    order = make_order(price="0.5", size="10")  # $5 notional
    rm.check_order(order, open_positions=[], open_orders_notional_usd=Decimal(0))


def test_rejects_oversize_order():
    rm = RiskManager(make_limits(max_position_usd=Decimal("10")))
    order = make_order(price="0.5", size="100")  # $50 notional vs $10 cap
    with pytest.raises(RiskRejected, match="max_position_usd"):
        rm.check_order(order, open_positions=[], open_orders_notional_usd=Decimal(0))


def test_rejects_when_total_exposure_exceeded():
    rm = RiskManager(make_limits(max_total_exposure_usd=Decimal("100")))
    open_pos = [Position(
        platform=Platform.POLYMARKET,
        market_id="OTHER",
        size=Decimal("100"),
        avg_entry=Decimal("0.9"),  # $90 notional
    )]
    order = make_order(price="0.5", size="40")  # +$20 → $110 > $100
    with pytest.raises(RiskRejected, match="max_total_exposure_usd"):
        rm.check_order(order, open_positions=open_pos, open_orders_notional_usd=Decimal(0))


def test_kill_switch_blocks_orders():
    rm = RiskManager(make_limits())
    rm.kill("manual test")
    order = make_order()
    with pytest.raises(RiskRejected, match="kill switch"):
        rm.check_order(order, open_positions=[], open_orders_notional_usd=Decimal(0))


def test_daily_loss_kills_bot():
    rm = RiskManager(make_limits(max_daily_loss_usd=Decimal("100")))
    rm.record_pnl_delta(Decimal("-50"))
    assert not rm.state.killed
    rm.record_pnl_delta(Decimal("-60"))
    assert rm.state.killed
    assert "daily loss" in (rm.state.kill_reason or "").lower()


def test_max_open_positions_allows_reducing_existing():
    rm = RiskManager(make_limits(max_open_positions=2))
    open_pos = [
        Position(platform=Platform.POLYMARKET, market_id="M1",
                 size=Decimal("10"), avg_entry=Decimal("0.5")),
        Position(platform=Platform.POLYMARKET, market_id="M2",
                 size=Decimal("10"), avg_entry=Decimal("0.5")),
    ]
    # Adding a third NEW market is rejected
    new_order = make_order(market_id="M3")
    with pytest.raises(RiskRejected, match="max_open_positions"):
        rm.check_order(new_order, open_positions=open_pos,
                       open_orders_notional_usd=Decimal(0))
    # But adding to an EXISTING market is allowed
    same_order = make_order(market_id="M1")
    rm.check_order(same_order, open_positions=open_pos,
                   open_orders_notional_usd=Decimal(0))


def test_daily_pnl_resets_on_new_day():
    rm = RiskManager(make_limits())
    rm.state.daily_pnl_usd = Decimal("-50")
    rm.state.daily_pnl_date = date.today() - timedelta(days=1)
    rm.record_pnl_delta(Decimal("-10"))
    # Should have rolled to today and the prior loss was discarded
    assert rm.state.daily_pnl_date == date.today()
    assert rm.state.daily_pnl_usd == Decimal("-10")
