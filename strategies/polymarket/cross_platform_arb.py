"""
Cross-platform arbitrage.

Compare Polymarket prices against Kalshi (US-regulated) and sportsbook
implied probabilities (via The Odds API). When the same event prices
materially differently, take the cheaper side on Polymarket and either
hedge or take the corresponding side on the other platform.

In practice, the trickiest part is *event matching*. "Will the Lakers
beat the Celtics on Jan 15?" looks the same to a human across platforms
but is keyed differently each place. v1 ships a manual mapping in
config; later we add fuzzy matching (LLM-assisted).

Sustainable forms:
- Sports moneyline arb (Kalshi has limited sports, sportsbooks have
  full coverage)
- Election market arb (Polymarket vs Kalshi)
- Weather/macro events shared across both prediction venues
"""
from __future__ import annotations

from decimal import Decimal

from core.models import Platform, Side, Signal, StrategyMode
from strategies.base import Strategy, StrategyContext


class CrossPlatformArbStrategy(Strategy):
    name = "polymarket_cross_platform_arb"

    def __init__(
        self,
        *,
        polymarket_exchange,
        event_map: list[dict],     # [{poly_token_id, kalshi_ticker, sportsbook_id}]
        mode: StrategyMode = StrategyMode.DISABLED,
        min_arb_edge: Decimal = Decimal("0.02"),
        per_trade_usd: Decimal = Decimal("25"),
    ):
        super().__init__(mode=mode)
        self.exchange = polymarket_exchange
        self.event_map = event_map
        self.min_edge = min_arb_edge
        self.per_trade_usd = per_trade_usd

    async def tick(self, ctx: StrategyContext) -> list[Signal]:
        if not self.enabled:
            return []
        # v1 stub: real impl wires a KalshiClient and OddsApiClient,
        # fetches implied probs for each mapped event, and emits a Signal
        # when |poly_prob - other_prob| > min_edge.
        self.log.info("cross_platform_arb.stub", mapped_events=len(self.event_map))
        return []
