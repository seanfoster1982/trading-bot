"""
Score-based benchmark strategy.

PURPOSE: This is a BASELINE for comparing real strategies against.
It is NOT a strategy with a thesis — it's a heuristic ranker that
scores markets by liquidity, volume, spread, and short-term price
movement, then signals on the top-N.

Use cases:
- Sanity check: if resolution_arb doesn't beat this in backtest,
  resolution_arb has no edge.
- Reference implementation of "what a naive score-based bot looks
  like" — useful for explaining why hand-wavy scoring isn't a strategy.

Why score-based ranking is NOT a real edge:
- A high score means a market is liquid and has tight spreads. That
  tells you it's a *good market to trade*, not that it's *mispriced*.
- "Probability movement" as a signal trades you INTO markets where
  new information just arrived. You're trading after the news, not
  before it. This is the opposite of edge.
- The composite score has no calibration. Why is liquidity weighted
  0.3 vs volume 0.2? Because someone picked numbers. There's no
  underlying probability theory.

Default mode: DISABLED. Even when enabled, treat its signals as a
noise floor, not as real opportunities.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.models import Platform, Side, Signal, StrategyMode
from strategies.base import Strategy, StrategyContext


@dataclass
class ScoreWeights:
    """Hand-picked weights. Don't read meaning into the values."""
    liquidity: float = 0.30
    volume_24h: float = 0.25
    spread_tightness: float = 0.25
    price_extremity: float = 0.20  # rewards markets near 0.5, where the
                                   # implied uncertainty is highest


def score_market(
    *,
    volume_24h: float,
    liquidity: float,
    spread: float | None = None,
    midpoint: float | None = None,
    weights: ScoreWeights | None = None,
) -> float:
    """Return a 0..1 score. Used by both this strategy and the dashboard.

    Each component is normalized via a saturating function so values
    don't blow out. None of this is theoretically motivated.
    """
    w = weights or ScoreWeights()

    # log1p saturating normalization. $10K liquidity ≈ 0.5; $100K ≈ 0.7
    def _saturate(x: float, scale: float) -> float:
        if x <= 0:
            return 0.0
        import math
        return min(1.0, math.log1p(x) / math.log1p(scale))

    liq_score = _saturate(liquidity, 100_000.0)
    vol_score = _saturate(volume_24h, 100_000.0)

    # Spread tightness: 1¢ spread → ~1.0; 10¢ → ~0.0
    if spread is None or spread <= 0:
        spread_score = 0.5  # unknown → neutral
    else:
        spread_score = max(0.0, 1.0 - (spread * 10))

    # Price extremity: closer to 0.5 → higher score (more uncertain)
    if midpoint is None:
        extremity_score = 0.5
    else:
        # Distance from 0.5, max distance is 0.5 → invert
        extremity_score = 1.0 - abs(midpoint - 0.5) * 2

    score = (
        w.liquidity * liq_score
        + w.volume_24h * vol_score
        + w.spread_tightness * spread_score
        + w.price_extremity * extremity_score
    )
    return round(score, 4)


class ScoreBasedStrategy(Strategy):
    name = "polymarket_score_based"

    def __init__(
        self,
        *,
        exchange,
        mode: StrategyMode = StrategyMode.DISABLED,
        score_threshold: float = 0.65,
        per_signal_usd: Decimal = Decimal("25"),
        min_volume_24h: Decimal = Decimal("1000"),
        max_spread: Decimal = Decimal("0.05"),
        side: Side = Side.BUY,
        weights: ScoreWeights | None = None,
    ):
        super().__init__(mode=mode)
        self.exchange = exchange
        self.threshold = score_threshold
        self.per_signal_usd = per_signal_usd
        self.min_volume_24h = min_volume_24h
        self.max_spread = max_spread
        self.side = side
        self.weights = weights or ScoreWeights()

    async def tick(self, ctx: StrategyContext) -> list[Signal]:
        if not self.enabled:
            return []

        markets = await self.exchange.list_markets(
            active_only=True, min_volume_24h=self.min_volume_24h,
        )
        signals: list[Signal] = []
        for m in markets:
            try:
                book = await self.exchange.get_order_book(m)
            except Exception:
                continue
            if book.best_ask is None or book.best_bid is None:
                continue

            spread = book.spread or Decimal("1")
            if spread > self.max_spread:
                continue
            mid = book.midpoint or Decimal("0.5")

            score = score_market(
                volume_24h=float(m.metadata.get("volume_24h") or 0),
                liquidity=float(m.metadata.get("liquidity") or 0),
                spread=float(spread),
                midpoint=float(mid),
                weights=self.weights,
            )
            if score < self.threshold:
                continue

            # Buy at the ask. The strategy doesn't have a directional
            # thesis — it just wants to be in liquid markets — so this
            # is essentially "hold a basket of high-score markets."
            target_price = book.best_ask
            signals.append(Signal(
                strategy=self.name,
                platform=Platform.POLYMARKET,
                market_id=m.market_id,
                token_id=m.token_id,
                side=self.side,
                target_price=target_price,
                target_size_usd=self.per_signal_usd,
                confidence=score,
                reason=f"score_based: score={score:.3f} (baseline)",
                metadata={"score": score, "spread": str(spread), "mid": str(mid)},
            ))

        self.log.info("score_based.signals", count=len(signals),
                      threshold=self.threshold)
        return signals
