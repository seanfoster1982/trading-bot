"""
Solana copy-trade.

Mirror Solana whale wallets via Helius/Solscan transaction streams.
Same logic as Polymarket copy-trade: edge depends on whale selection
and discipline about cutting whales whose recent PnL turns south.

v1 stub. Real impl: subscribe to the wallets via Helius webhooks or
a getSignaturesForAddress poller, decode swap instructions, emit
matching swap signals into our Solana exchange.
"""
from __future__ import annotations

from decimal import Decimal

from core.models import Signal, StrategyMode
from strategies.base import Strategy, StrategyContext


class SolanaCopyTradeStrategy(Strategy):
    name = "solana_copy_trade"

    def __init__(
        self,
        *,
        solana_exchange,
        wallet_addresses: list[str],
        mode: StrategyMode = StrategyMode.DISABLED,
        per_trade_usd: Decimal = Decimal("25"),
    ):
        super().__init__(mode=mode)
        self.exchange = solana_exchange
        self.wallets = wallet_addresses
        self.per_trade_usd = per_trade_usd

    async def tick(self, ctx: StrategyContext) -> list[Signal]:
        if not self.enabled:
            return []
        self.log.info("solana_copy_trade.stub", wallet_count=len(self.wallets))
        return []
