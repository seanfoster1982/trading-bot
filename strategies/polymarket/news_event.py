"""
News-driven event strategy.

Read wire feeds. When breaking news materially changes the probability
of a Polymarket-listed event, reposition before the rest of the crowd.

Why this is the hardest of the four:
- "Material" is hard to define. Most headlines don't change probabilities;
  the bot will fire too often or too rarely.
- LLMs hallucinate event mappings. "Trump indicted" and "Trump indicted
  for the third time" are different markets.
- The crowd often beats news API delivery — Twitter/X moves before wires.
- Backtesting requires reconstructing what news was available at each
  historical timestamp, which is brutally tedious.

Architecture:
- News source fan-in: NewsAPI + RSS feeds + (optionally) X/Twitter
- LLM (Claude) gets headline + relevant Polymarket question, returns
  estimated probability and confidence
- We act only when |estimated_prob - market_prob| > threshold AND
  confidence is high
- Hard cap on signals per hour to prevent runaway

v1 ships as a stub. Wire it up only after you have the simpler
strategies working — this is the one most likely to lose money.
"""
from __future__ import annotations

from decimal import Decimal

from core.models import Signal, StrategyMode
from strategies.base import Strategy, StrategyContext


class NewsEventStrategy(Strategy):
    name = "polymarket_news_event"

    def __init__(
        self,
        *,
        polymarket_exchange,
        anthropic_api_key: str,
        news_api_key: str,
        mode: StrategyMode = StrategyMode.DISABLED,
        min_edge: Decimal = Decimal("0.05"),
        min_confidence: float = 0.7,
        max_signals_per_hour: int = 5,
    ):
        super().__init__(mode=mode)
        self.exchange = polymarket_exchange
        self.anthropic_key = anthropic_api_key
        self.news_key = news_api_key
        self.min_edge = min_edge
        self.min_confidence = min_confidence
        self.max_per_hour = max_signals_per_hour

    async def tick(self, ctx: StrategyContext) -> list[Signal]:
        if not self.enabled:
            return []
        self.log.info("news_event.stub")
        return []
