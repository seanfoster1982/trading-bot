"""
Polymarket CLOB exchange adapter.

Wraps py-clob-client with our Exchange interface. py-clob-client is sync;
we run it in a threadpool via asyncio.to_thread so the rest of the bot
stays async.

Auth model:
- L1 = wallet signature (EIP-712), needed once to derive API creds
- L2 = HMAC API creds, used for every order/cancel call

We auto-derive on first use and cache. If you want to pin to existing
creds, pass them via env (CLOB_API_KEY etc.) and set_api_creds yourself.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from decimal import Decimal
from typing import AsyncIterator

import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

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
)
from exchanges.base import Exchange
from exchanges.polymarket.gamma import GammaClient

log = structlog.get_logger(__name__)


class PolymarketExchange(Exchange):
    platform = Platform.POLYMARKET

    def __init__(
        self,
        *,
        host: str,
        gamma_host: str,
        chain_id: int,
        private_key: str,
        funder_address: str,
        signature_type: int = 1,
    ):
        self._host = host
        self._gamma_host = gamma_host
        self._chain_id = chain_id
        self._private_key = private_key
        self._funder = funder_address
        self._sig_type = signature_type
        self._client = None  # lazy init
        self._gamma = GammaClient(gamma_host)

    # ----- lifecycle -------------------------------------------------------

    async def connect(self) -> None:
        """Initialize the underlying py-clob-client and derive API creds."""
        if self._client is not None:
            return
        # Imports deferred so the package can be imported without the SDK
        # in environments where it isn't installed yet (e.g. test harness)
        from py_clob_client.client import ClobClient

        def _build():
            client = ClobClient(
                self._host,
                key=self._private_key,
                chain_id=self._chain_id,
                signature_type=self._sig_type,
                funder=self._funder,
            )
            client.set_api_creds(client.create_or_derive_api_creds())
            return client

        self._client = await asyncio.to_thread(_build)
        log.info("polymarket.connected", host=self._host, chain_id=self._chain_id)

    async def close(self) -> None:
        await self._gamma.close()

    def _require(self):
        if self._client is None:
            raise RuntimeError("PolymarketExchange.connect() must be awaited first")
        return self._client

    # ----- read ------------------------------------------------------------

    async def list_markets(
        self,
        *,
        active_only: bool = True,
        min_volume_24h: Decimal | None = None,
    ) -> list[Market]:
        return await self._gamma.list_markets(
            active=active_only,
            closed=not active_only,
            min_volume_24h=min_volume_24h,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def get_order_book(self, market: Market) -> OrderBook:
        if market.token_id is None:
            raise ValueError(f"market {market.market_id} has no token_id")
        client = self._require()
        raw = await asyncio.to_thread(client.get_order_book, market.token_id)
        bids = [
            OrderBookLevel(price=Decimal(str(b.price)), size=Decimal(str(b.size)))
            for b in (raw.bids or [])
        ]
        asks = [
            OrderBookLevel(price=Decimal(str(a.price)), size=Decimal(str(a.size)))
            for a in (raw.asks or [])
        ]
        # py-clob-client returns bids ascending and asks descending
        # in some versions; normalize.
        bids.sort(key=lambda l: l.price, reverse=True)
        asks.sort(key=lambda l: l.price)
        return OrderBook(
            market_id=market.market_id,
            bids=bids,
            asks=asks,
            timestamp=datetime.utcnow(),
        )

    async def get_balance_usd(self) -> Decimal:
        # Polymarket balance lookup goes through a separate USDC contract
        # call on Polygon; py-clob-client doesn't ship a balance helper.
        # For paper-trading scaffold, return 0; live impl wires web3.py.
        log.debug("polymarket.balance_not_implemented")
        return Decimal(0)

    async def get_positions(self) -> list[Position]:
        # Real implementation reads the user's positions endpoint or
        # derives from on-chain conditional token balances. Stub for now.
        log.debug("polymarket.positions_not_implemented")
        return []

    # ----- write -----------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def submit_order(self, order: Order) -> Order:
        client = self._require()
        from py_clob_client.clob_types import OrderArgs, OrderType as ClobOrderType

        if order.token_id is None:
            order.status = OrderStatus.REJECTED
            order.error = "missing token_id"
            return order

        side_map = {Side.BUY: "BUY", Side.SELL: "SELL"}
        type_map = {
            OrderType.GTC: ClobOrderType.GTC,
            OrderType.FOK: ClobOrderType.FOK,
            OrderType.FAK: ClobOrderType.FAK,
        }
        if order.order_type not in type_map:
            order.status = OrderStatus.REJECTED
            order.error = f"order_type {order.order_type} not supported on Polymarket"
            return order

        args = OrderArgs(
            token_id=order.token_id,
            price=float(order.price),
            size=float(order.size),
            side=side_map[order.side],
        )

        def _submit():
            signed = client.create_order(args)
            return client.post_order(signed, type_map[order.order_type])

        try:
            resp = await asyncio.to_thread(_submit)
            order.id = str(resp.get("orderID") or resp.get("id") or "")
            success = bool(resp.get("success", False)) or bool(order.id)
            order.status = OrderStatus.OPEN if success else OrderStatus.REJECTED
            if not success:
                order.error = str(resp.get("errorMsg") or resp)
            order.updated_at = datetime.utcnow()
            log.info(
                "polymarket.order_submitted",
                client_order_id=order.client_order_id,
                platform_id=order.id,
                status=order.status,
            )
        except Exception as e:
            order.status = OrderStatus.FAILED
            order.error = str(e)
            order.updated_at = datetime.utcnow()
            log.exception("polymarket.order_failed", client_order_id=order.client_order_id)
        return order

    async def cancel_order(self, order: Order) -> bool:
        if not order.id:
            return False
        client = self._require()
        try:
            resp = await asyncio.to_thread(client.cancel, order_id=order.id)
            log.info("polymarket.cancelled", platform_id=order.id, resp=resp)
            return True
        except Exception:
            log.exception("polymarket.cancel_failed", platform_id=order.id)
            return False

    async def cancel_all(self) -> int:
        client = self._require()
        try:
            resp = await asyncio.to_thread(client.cancel_all)
            cancelled = len(resp.get("canceled", []) if isinstance(resp, dict) else resp or [])
            log.warning("polymarket.cancel_all", count=cancelled)
            return cancelled
        except Exception:
            log.exception("polymarket.cancel_all_failed")
            return 0

    # ----- streaming -------------------------------------------------------

    async def stream_fills(self) -> AsyncIterator[Fill]:
        # Real implementation uses Polymarket WebSocket /user channel.
        # Scaffold stub: do nothing forever, allowing the engine to start.
        log.debug("polymarket.stream_fills_stub")
        while True:
            await asyncio.sleep(60)
            if False:
                yield  # type: ignore[unreachable]


def new_client_order_id() -> str:
    return f"pm-{uuid.uuid4().hex[:16]}"
