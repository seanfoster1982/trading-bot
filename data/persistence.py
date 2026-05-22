"""
Persistence service. Wraps SQLAlchemy writes so the engine, backtester,
and any future module can record activity without coupling to schema details.

Design:
- One service instance per process. Caller owns lifecycle (close on shutdown).
- All writes are sync — SQLAlchemy 2.x sync API. Engine wraps calls in
  asyncio.to_thread to keep the main loop responsive.
- Failures are logged but do NOT crash the trading loop. Persistence is
  observability, not correctness; losing a row is preferable to halting trading.
- SQLite WAL mode is enabled for concurrent read+write (dashboard reads while
  engine writes). Postgres ignores the pragma cleanly.

Tables touched (defined in data/schema.py):
- orders         every order attempted (paper or live)
- fills          executions, parent → orders
- positions      current holdings derived from fills
- equity_points  equity snapshots
- alerts         significant events (kill switch, large losses)
- markets        cached metadata
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Iterable

import structlog
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine as SAEngine
from sqlalchemy.orm import Session, sessionmaker

from core.models import (
    Fill,
    Market,
    Order,
    OrderStatus,
    Platform,
    Position,
    Side,
)
from data.schema import (
    AlertRow,
    Base,
    EquityPointRow,
    FillRow,
    MarketRow,
    OrderRow,
    PositionRow,
)

log = structlog.get_logger(__name__)


def _enable_sqlite_wal(engine: SAEngine) -> None:
    """SQLite-only: WAL lets the dashboard read while the engine writes."""
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_conn, conn_record):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()


class Persistence:
    def __init__(self, database_url: str, *, echo: bool = False):
        # SQLite needs check_same_thread=False because we hand sessions
        # across asyncio.to_thread boundaries
        connect_args = {}
        if database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        self.engine = create_engine(
            database_url, echo=echo, future=True, connect_args=connect_args
        )
        _enable_sqlite_wal(self.engine)
        self.Session: sessionmaker[Session] = sessionmaker(
            self.engine, expire_on_commit=False, future=True
        )

    # ----- schema -----------------------------------------------------------

    def init_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    # ----- orders + fills ---------------------------------------------------

    def record_order(self, order: Order) -> int | None:
        """Insert or update an order keyed on client_order_id. Returns DB id."""
        try:
            with self.Session() as s, s.begin():
                row = s.scalar(
                    select(OrderRow).where(
                        OrderRow.client_order_id == order.client_order_id
                    )
                )
                if row is None:
                    row = OrderRow(
                        client_order_id=order.client_order_id,
                        platform_order_id=order.id,
                        strategy=order.strategy,
                        platform=order.platform.value,
                        market_id=order.market_id,
                        token_id=order.token_id,
                        side=order.side.value,
                        order_type=order.order_type.value,
                        price=order.price,
                        size=order.size,
                        status=order.status.value,
                        is_paper=order.is_paper,
                        error=order.error,
                        created_at=order.created_at,
                        updated_at=order.updated_at,
                    )
                    s.add(row)
                    s.flush()
                    return row.id
                # update mutable fields
                row.platform_order_id = order.id or row.platform_order_id
                row.status = order.status.value
                row.error = order.error
                row.updated_at = order.updated_at
                s.flush()
                return row.id
        except Exception:
            log.exception("persistence.record_order_failed",
                          client_order_id=order.client_order_id)
            return None

    def record_fill(self, fill: Fill, order_db_id: int) -> int | None:
        try:
            with self.Session() as s, s.begin():
                row = FillRow(
                    order_id=order_db_id,
                    platform=fill.platform.value,
                    market_id=fill.market_id,
                    side=fill.side.value,
                    price=fill.price,
                    size=fill.size,
                    fee=fill.fee,
                    timestamp=fill.timestamp,
                )
                s.add(row)
                s.flush()
                return row.id
        except Exception:
            log.exception("persistence.record_fill_failed")
            return None

    def record_paper_fill(self, order: Order, *, fee: Decimal = Decimal(0)) -> None:
        """Convenience: record an order's row, then a synthetic fill at the
        order's price. Used by the engine in paper mode where the order is
        immediately considered filled at its limit price."""
        db_id = self.record_order(order)
        if db_id is None:
            return
        fill = Fill(
            order_client_id=order.client_order_id,
            platform=order.platform,
            market_id=order.market_id,
            side=order.side,
            price=order.price,
            size=order.size,
            fee=fee,
            timestamp=datetime.utcnow(),
        )
        self.record_fill(fill, db_id)
        self._upsert_position_from_fill(order, fill)

    # ----- positions --------------------------------------------------------

    def _upsert_position_from_fill(self, order: Order, fill: Fill) -> None:
        """Adjust open position for the (platform, market_id) pair.
        Simple model: BUY adds, SELL subtracts. Re-averages entry on adds.
        Closed positions are kept with closed_at set."""
        try:
            with self.Session() as s, s.begin():
                pos = s.scalar(
                    select(PositionRow).where(
                        PositionRow.platform == order.platform.value,
                        PositionRow.market_id == order.market_id,
                        PositionRow.closed_at.is_(None),
                    )
                )
                signed_size = fill.size if order.side == Side.BUY else -fill.size
                if pos is None:
                    if signed_size == 0:
                        return
                    pos = PositionRow(
                        platform=order.platform.value,
                        market_id=order.market_id,
                        token_id=order.token_id,
                        size=signed_size,
                        avg_entry=fill.price,
                        strategy=order.strategy,
                    )
                    s.add(pos)
                    return

                new_size = pos.size + signed_size
                if signed_size > 0 and pos.size > 0:
                    # Adding to long: weighted average
                    total_cost = pos.size * pos.avg_entry + signed_size * fill.price
                    pos.avg_entry = total_cost / new_size
                # Reducing or flipping: realize PnL on the reduced portion
                elif (signed_size < 0 and pos.size > 0) or (signed_size > 0 and pos.size < 0):
                    closed_amount = min(abs(signed_size), abs(pos.size))
                    if pos.size > 0:
                        realized = closed_amount * (fill.price - pos.avg_entry)
                    else:
                        realized = closed_amount * (pos.avg_entry - fill.price)
                    pos.realized_pnl = (pos.realized_pnl or Decimal(0)) + realized

                pos.size = new_size
                if abs(new_size) < Decimal("0.0001"):
                    pos.size = Decimal(0)
                    pos.closed_at = datetime.utcnow()
        except Exception:
            log.exception("persistence.position_update_failed",
                          market=order.market_id)

    def list_open_positions(self) -> list[PositionRow]:
        with self.Session() as s:
            return list(s.scalars(
                select(PositionRow).where(PositionRow.closed_at.is_(None))
            ))

    # ----- equity -----------------------------------------------------------

    def record_equity_point(
        self,
        *,
        cash_usd: Decimal,
        positions_usd: Decimal,
        total_equity_usd: Decimal,
        timestamp: datetime | None = None,
    ) -> None:
        try:
            with self.Session() as s, s.begin():
                s.add(EquityPointRow(
                    timestamp=timestamp or datetime.utcnow(),
                    cash_usd=cash_usd,
                    positions_usd=positions_usd,
                    total_equity_usd=total_equity_usd,
                ))
        except Exception:
            log.exception("persistence.equity_failed")

    # ----- markets ----------------------------------------------------------

    def upsert_market(self, market: Market) -> None:
        try:
            with self.Session() as s, s.begin():
                row = s.scalar(
                    select(MarketRow).where(
                        MarketRow.platform == market.platform.value,
                        MarketRow.market_id == market.market_id,
                    )
                )
                meta_json = json.dumps(
                    {k: (str(v) if isinstance(v, Decimal) else v)
                     for k, v in (market.metadata or {}).items()},
                    default=str,
                )
                if row is None:
                    s.add(MarketRow(
                        platform=market.platform.value,
                        market_id=market.market_id,
                        token_id=market.token_id,
                        question=market.question,
                        outcome=market.outcome,
                        end_date=market.end_date,
                        metadata_json=meta_json,
                    ))
                else:
                    row.question = market.question
                    row.outcome = market.outcome
                    row.end_date = market.end_date
                    row.metadata_json = meta_json
                    row.last_seen = datetime.utcnow()
        except Exception:
            log.exception("persistence.market_upsert_failed",
                          market=market.market_id)

    # ----- alerts -----------------------------------------------------------

    def record_alert(
        self, *, severity: str, source: str, message: str,
        metadata: dict | None = None,
    ) -> None:
        try:
            with self.Session() as s, s.begin():
                s.add(AlertRow(
                    severity=severity,
                    source=source,
                    message=message,
                    metadata_json=json.dumps(metadata or {}, default=str),
                ))
        except Exception:
            log.exception("persistence.alert_failed")

    # ----- read helpers (used by dashboard) ---------------------------------

    def fetch_orders(self, *, limit: int = 200,
                     strategy: str | None = None,
                     paper_only: bool = False) -> list[OrderRow]:
        with self.Session() as s:
            q = select(OrderRow).order_by(OrderRow.created_at.desc()).limit(limit)
            if strategy:
                q = q.where(OrderRow.strategy == strategy)
            if paper_only:
                q = q.where(OrderRow.is_paper.is_(True))
            return list(s.scalars(q))

    def fetch_fills(self, *, limit: int = 500) -> list[FillRow]:
        with self.Session() as s:
            return list(s.scalars(
                select(FillRow).order_by(FillRow.timestamp.desc()).limit(limit)
            ))

    def fetch_equity_curve(self, *, limit: int = 5000) -> list[EquityPointRow]:
        with self.Session() as s:
            return list(s.scalars(
                select(EquityPointRow).order_by(EquityPointRow.timestamp.asc()).limit(limit)
            ))

    def fetch_alerts(self, *, limit: int = 100) -> list[AlertRow]:
        with self.Session() as s:
            return list(s.scalars(
                select(AlertRow).order_by(AlertRow.timestamp.desc()).limit(limit)
            ))


# Module-level singleton helper. Engine and dashboard both call get_persistence()
# with the same DATABASE_URL and get the same instance.
_singleton: Persistence | None = None
_singleton_url: str | None = None


def get_persistence(database_url: str) -> Persistence:
    global _singleton, _singleton_url
    if _singleton is None or _singleton_url != database_url:
        if _singleton is not None:
            try:
                _singleton.close()
            except Exception:
                pass
        _singleton = Persistence(database_url)
        _singleton_url = database_url
        _singleton.init_schema()
    return _singleton
