"""
Persistence tests. Covers:

- Schema initializes (SQLite in-memory)
- Order writes are upsert: same client_order_id updates, doesn't duplicate
- Paper fill records both order and fill, updates open position
- BUY then BUY averages entry; BUY then SELL realizes PnL and closes
- Equity points round-trip
- Risk-rejected order is still recorded (visible in dashboard)
- Read helpers return shapes the dashboard expects
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from core.models import (
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Platform,
    Side,
)
from data.persistence import Persistence


@pytest.fixture
def db() -> Persistence:
    p = Persistence("sqlite:///:memory:")
    p.init_schema()
    yield p
    p.close()


def make_order(
    *, client_id: str = "test-1", price: str = "0.50", size: str = "20",
    side: Side = Side.BUY, market_id: str = "M1",
    status: OrderStatus = OrderStatus.FILLED, is_paper: bool = True,
    strategy: str = "test_strategy",
) -> Order:
    return Order(
        client_order_id=client_id,
        strategy=strategy,
        platform=Platform.POLYMARKET,
        market_id=market_id,
        token_id="T1",
        side=side,
        order_type=OrderType.GTC,
        price=Decimal(price),
        size=Decimal(size),
        status=status,
        is_paper=is_paper,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )


# ----- schema + basic writes ---------------------------------------------

def test_schema_creates_all_tables(db: Persistence):
    """All six tables should exist in a fresh SQLite database."""
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    tables = set(insp.get_table_names())
    assert {"orders", "fills", "positions", "equity_points",
            "alerts", "markets"}.issubset(tables)


def test_record_order_returns_db_id(db: Persistence):
    order = make_order()
    db_id = db.record_order(order)
    assert isinstance(db_id, int) and db_id > 0


def test_record_order_is_upsert(db: Persistence):
    order = make_order(client_id="dup-1", status=OrderStatus.PENDING)
    id1 = db.record_order(order)

    # Re-record with updated status — should NOT create a new row
    order.status = OrderStatus.FILLED
    order.error = None
    id2 = db.record_order(order)

    assert id1 == id2
    fetched = db.fetch_orders(limit=10)
    assert len(fetched) == 1
    assert fetched[0].status == "filled"


# ----- paper fill flow ---------------------------------------------------

def test_paper_fill_records_order_fill_and_position(db: Persistence):
    order = make_order(client_id="paper-1", price="0.40", size="50")
    db.record_paper_fill(order)

    orders = db.fetch_orders(paper_only=True)
    fills = db.fetch_fills()
    positions = db.list_open_positions()

    assert len(orders) == 1
    assert len(fills) == 1
    assert len(positions) == 1
    assert positions[0].size == Decimal("50")
    assert positions[0].avg_entry == Decimal("0.40")


def test_two_buys_average_entry(db: Persistence):
    db.record_paper_fill(make_order(client_id="buy-1", price="0.40", size="50"))
    db.record_paper_fill(make_order(client_id="buy-2", price="0.50", size="50"))
    positions = db.list_open_positions()
    assert len(positions) == 1
    assert positions[0].size == Decimal("100")
    # weighted avg: (50*0.40 + 50*0.50) / 100 = 0.45
    assert positions[0].avg_entry == Decimal("0.45")


def test_buy_then_sell_realizes_pnl_and_closes(db: Persistence):
    db.record_paper_fill(make_order(
        client_id="entry", price="0.40", size="50", side=Side.BUY,
    ))
    db.record_paper_fill(make_order(
        client_id="exit", price="0.60", size="50", side=Side.SELL,
    ))
    positions = db.list_open_positions()
    # After full close, no open positions remain
    assert positions == []


def test_partial_close_keeps_position_open(db: Persistence):
    db.record_paper_fill(make_order(
        client_id="entry", price="0.40", size="100", side=Side.BUY,
    ))
    db.record_paper_fill(make_order(
        client_id="partial", price="0.60", size="40", side=Side.SELL,
    ))
    positions = db.list_open_positions()
    assert len(positions) == 1
    # Remaining size 60, realized PnL on 40 closed = 40 * (0.60 - 0.40) = 8
    assert positions[0].size == Decimal("60")
    assert positions[0].realized_pnl == Decimal("8.00")


# ----- equity ------------------------------------------------------------

def test_equity_points_round_trip(db: Persistence):
    base = datetime.utcnow()
    db.record_equity_point(
        cash_usd=Decimal("1000"),
        positions_usd=Decimal("0"),
        total_equity_usd=Decimal("1000"),
        timestamp=base,
    )
    db.record_equity_point(
        cash_usd=Decimal("950"),
        positions_usd=Decimal("60"),
        total_equity_usd=Decimal("1010"),
        timestamp=base + timedelta(minutes=5),
    )
    points = db.fetch_equity_curve()
    assert len(points) == 2
    # Returned in ascending order
    assert points[0].total_equity_usd == Decimal("1000")
    assert points[1].total_equity_usd == Decimal("1010")


# ----- rejected order is still persisted (dashboard visibility) ---------

def test_rejected_order_records_for_dashboard_visibility(db: Persistence):
    """Risk-rejected orders should be in the DB so the dashboard can show
    why a strategy isn't placing trades."""
    order = make_order(
        client_id="rej-1", status=OrderStatus.REJECTED,
    )
    order.error = "risk: max_position_usd"
    db.record_order(order)

    orders = db.fetch_orders()
    assert len(orders) == 1
    assert orders[0].status == "rejected"
    assert "max_position_usd" in (orders[0].error or "")


# ----- shape sanity for dashboard ----------------------------------------

def test_fetch_orders_strategy_filter(db: Persistence):
    db.record_paper_fill(make_order(client_id="a", strategy="strat_a"))
    db.record_paper_fill(make_order(client_id="b", strategy="strat_b",
                                    market_id="M2"))
    only_a = db.fetch_orders(strategy="strat_a")
    only_b = db.fetch_orders(strategy="strat_b")
    assert len(only_a) == 1
    assert len(only_b) == 1
    assert only_a[0].strategy == "strat_a"
    assert only_b[0].strategy == "strat_b"


def test_fetch_orders_paper_only_filter(db: Persistence):
    db.record_order(make_order(client_id="paper", is_paper=True))
    db.record_order(make_order(client_id="live",  is_paper=False,
                               market_id="M2"))
    paper = db.fetch_orders(paper_only=True)
    assert len(paper) == 1
    assert paper[0].is_paper is True


def test_alert_recording(db: Persistence):
    db.record_alert(severity="critical", source="risk",
                    message="kill switch engaged",
                    metadata={"reason": "daily loss"})
    alerts = db.fetch_alerts()
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
    assert "kill switch" in alerts[0].message
