"""
Solana exchange adapter. Trades through Jupiter aggregator.

A "market" on Solana is a token pair (input_mint, output_mint). Order book
isn't applicable the same way — Jupiter returns a quote for a specific size.
We synthesize a simple book from quotes at a few sizes for strategies that
expect one.

Limit orders aren't native on Solana DEXes (Jupiter Limit Orders exist as
a separate product). v1 supports MARKET only via swap.
"""
from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import datetime
from decimal import Decimal
from typing import AsyncIterator

import structlog

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
from exchanges.solana.jupiter import JupiterClient

log = structlog.get_logger(__name__)


# Common mints. Strategies will reference these by symbol → mint.
COMMON_MINTS: dict[str, str] = {
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "SOL": "So11111111111111111111111111111111111111112",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}


class SolanaExchange(Exchange):
    platform = Platform.SOLANA

    def __init__(
        self,
        *,
        rpc_url: str,
        jupiter_host: str,
        private_key_b58: str,
    ):
        self._rpc_url = rpc_url
        self._jupiter = JupiterClient(jupiter_host)
        self._private_key_b58 = private_key_b58
        self._keypair = None      # solders Keypair, lazy
        self._public_key = None
        self._rpc = None          # AsyncClient, lazy

    async def connect(self) -> None:
        if self._keypair is not None:
            return
        from solders.keypair import Keypair
        from solana.rpc.async_api import AsyncClient

        self._keypair = Keypair.from_base58_string(self._private_key_b58)
        self._public_key = str(self._keypair.pubkey())
        self._rpc = AsyncClient(self._rpc_url)
        log.info("solana.connected", public_key=self._public_key)

    async def close(self):
        await self._jupiter.close()
        if self._rpc is not None:
            await self._rpc.close()

    # ----- read ------------------------------------------------------------

    async def list_markets(
        self,
        *,
        active_only: bool = True,
        min_volume_24h: Decimal | None = None,
    ) -> list[Market]:
        # v1: hardcoded major pairs against USDC. Real impl pulls from
        # Birdeye, Jupiter token list, or DexScreener for dynamic universe.
        majors = ["SOL", "USDT"]
        out: list[Market] = []
        for sym in majors:
            mint = COMMON_MINTS.get(sym)
            if not mint:
                continue
            out.append(Market(
                platform=Platform.SOLANA,
                market_id=f"{sym}/USDC",
                token_id=mint,
                question=None,
                outcome=sym,
                metadata={
                    "input_mint": COMMON_MINTS["USDC"],
                    "output_mint": mint,
                    "symbol": sym,
                },
            ))
        return out

    async def get_order_book(self, market: Market) -> OrderBook:
        """Synthesize a book from Jupiter quotes at a few sizes."""
        in_mint = market.metadata.get("input_mint")
        out_mint = market.metadata.get("output_mint")
        if not in_mint or not out_mint:
            raise ValueError(f"market {market.market_id} missing mints")

        # Probe at ~$10, $100, $1000 USDC (USDC has 6 decimals)
        sizes_atomic = [10_000_000, 100_000_000, 1_000_000_000]
        bids: list[OrderBookLevel] = []
        asks: list[OrderBookLevel] = []
        for atom in sizes_atomic:
            buy_q = await self._jupiter.quote(
                input_mint=in_mint, output_mint=out_mint, amount_atomic=atom,
            )
            sell_q = await self._jupiter.quote(
                input_mint=out_mint, output_mint=in_mint, amount_atomic=atom,
            )
            # Effective ask: USDC paid / token received
            in_amt = Decimal(buy_q["inAmount"])
            out_amt = Decimal(buy_q["outAmount"])
            if out_amt > 0:
                asks.append(OrderBookLevel(
                    price=in_amt / out_amt,
                    size=out_amt,
                ))
            in_amt2 = Decimal(sell_q["inAmount"])
            out_amt2 = Decimal(sell_q["outAmount"])
            if in_amt2 > 0:
                bids.append(OrderBookLevel(
                    price=out_amt2 / in_amt2,
                    size=in_amt2,
                ))
        bids.sort(key=lambda l: l.price, reverse=True)
        asks.sort(key=lambda l: l.price)
        return OrderBook(
            market_id=market.market_id,
            bids=bids,
            asks=asks,
            timestamp=datetime.utcnow(),
        )

    async def get_balance_usd(self) -> Decimal:
        # Real impl: getTokenAccountsByOwner for USDC ATA + SOL native balance.
        # Stub for scaffold.
        log.debug("solana.balance_not_implemented")
        return Decimal(0)

    async def get_positions(self) -> list[Position]:
        log.debug("solana.positions_not_implemented")
        return []

    # ----- write -----------------------------------------------------------

    async def submit_order(self, order: Order) -> Order:
        if order.order_type != OrderType.MARKET:
            order.status = OrderStatus.REJECTED
            order.error = "Solana adapter v1 supports MARKET only"
            return order

        in_mint = order.metadata.get("input_mint")
        out_mint = order.metadata.get("output_mint")
        in_decimals = int(order.metadata.get("input_decimals", 6))
        if not in_mint or not out_mint:
            order.status = OrderStatus.REJECTED
            order.error = "missing input_mint/output_mint in order metadata"
            return order

        # `size` here is interpreted as input amount in human units
        amount_atomic = int(order.size * (Decimal(10) ** in_decimals))

        try:
            quote = await self._jupiter.quote(
                input_mint=in_mint,
                output_mint=out_mint,
                amount_atomic=amount_atomic,
                slippage_bps=int(order.metadata.get("slippage_bps", 50)),
            )
            tx_b64 = await self._jupiter.swap(
                quote_response=quote,
                user_public_key=self._public_key,
            )
            sig = await self._sign_and_submit(tx_b64)
            order.id = sig
            order.status = OrderStatus.FILLED
            order.updated_at = datetime.utcnow()
            log.info("solana.swap_submitted", sig=sig, market=order.market_id)
        except Exception as e:
            order.status = OrderStatus.FAILED
            order.error = str(e)
            order.updated_at = datetime.utcnow()
            log.exception("solana.swap_failed", market=order.market_id)
        return order

    async def cancel_order(self, order: Order) -> bool:
        # Solana swaps are atomic — once submitted, they fill or fail.
        # No cancel.
        return False

    async def cancel_all(self) -> int:
        return 0

    async def stream_fills(self) -> AsyncIterator[Fill]:
        log.debug("solana.stream_fills_stub")
        while True:
            await asyncio.sleep(60)
            if False:
                yield  # type: ignore[unreachable]

    # ----- internal --------------------------------------------------------

    async def _sign_and_submit(self, tx_b64: str) -> str:
        """Sign Jupiter's versioned tx and submit via RPC. Returns signature."""
        from solders.transaction import VersionedTransaction
        from solana.rpc.types import TxOpts

        raw = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(raw)
        # Sign with our keypair. For VersionedTransaction we rebuild signed.
        signed = VersionedTransaction(tx.message, [self._keypair])
        resp = await self._rpc.send_raw_transaction(
            bytes(signed),
            opts=TxOpts(skip_preflight=False, preflight_commitment="processed"),
        )
        return str(resp.value)


def new_client_order_id() -> str:
    return f"sol-{uuid.uuid4().hex[:16]}"
