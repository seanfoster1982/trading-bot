"""
Copy-trade Polymarket whales.

Edge depends entirely on picking the right wallets. v1 ships as a
parameterized stub: pass a list of wallet addresses, the strategy polls
the Polymarket data API for their recent trades and emits matching
signals. We size proportional to our own bankroll, not theirs.

The hard work is *whale selection*. Use Dune Analytics queries against
Polymarket data to find wallets with consistent profitability across
many markets (not one big lucky bet). Refresh quarterly. Stop copying
the moment a wallet's rolling 30-day PnL turns negative.

Risks:
- Whales can be wrong, and often are. They just need to be right more
  often than they're wrong, weighted by sizing.
- A whale who got lucky once will look like a whale until they aren't.
- Front-running: if everyone copies the same whale, you all push price
  against yourselves. Pick less-watched wallets.
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import structlog

from core.models import Platform, Side, Signal, StrategyMode
from strategies.base import Strategy, StrategyContext

log = structlog.get_logger(__name__)


class PolymarketCopyTradeStrategy(Strategy):
    name = "polymarket_copy_trade"

    DATA_API_HOST = "https://data-api.polymarket.com"

    def __init__(
        self,
        *,
        exchange,
        wallet_addresses: list[str],
        mode: StrategyMode = StrategyMode.DISABLED,
        per_trade_usd: Decimal = Decimal("25"),
        min_whale_trade_usd: Decimal = Decimal("500"),
        lookback_minutes: int = 5,
    ):
        super().__init__(mode=mode)
        self.exchange = exchange
        self.wallets = [w.lower() for w in wallet_addresses]
        self.per_trade_usd = per_trade_usd
        self.min_whale_trade_usd = min_whale_trade_usd
        self.lookback_minutes = lookback_minutes
        self._seen_trade_ids: set[str] = set()

    async def tick(self, ctx: StrategyContext) -> list[Signal]:
        if not self.enabled:
            return []
        signals: list[Signal] = []
        async with httpx.AsyncClient(base_url=self.DATA_API_HOST, timeout=10.0) as c:
            for wallet in self.wallets:
                try:
                    r = await c.get("/trades", params={"user": wallet, "limit": 25})
                    r.raise_for_status()
                    trades = r.json() or []
                except Exception:
                    self.log.warning("copy_trade.fetch_failed", wallet=wallet)
                    continue
                for t in trades:
                    tid = str(t.get("transactionHash") or t.get("id") or "")
                    if not tid or tid in self._seen_trade_ids:
                        continue
                    self._seen_trade_ids.add(tid)
                    notional = Decimal(str(t.get("size", 0))) * Decimal(str(t.get("price", 0)))
                    if notional < self.min_whale_trade_usd:
                        continue
                    side = Side.BUY if str(t.get("side", "BUY")).upper() == "BUY" else Side.SELL
                    token_id = str(t.get("asset") or t.get("tokenId") or "")
                    market_id = str(t.get("market") or t.get("conditionId") or "")
                    price = Decimal(str(t.get("price", 0)))
                    if not token_id or not market_id or price <= 0:
                        continue
                    signals.append(Signal(
                        strategy=self.name,
                        platform=Platform.POLYMARKET,
                        market_id=market_id,
                        token_id=token_id,
                        side=side,
                        target_price=price,
                        target_size_usd=self.per_trade_usd,
                        confidence=0.6,
                        reason=f"copy_trade: whale {wallet[:6]} {side} ${notional:.0f}",
                        metadata={"whale": wallet, "whale_tx": tid},
                    ))
        self.log.info("copy_trade.signals", count=len(signals))
        return signals
