"""
Resolution arbitrage.

Edge: Polymarket markets close to certain resolution but trading at 0.97/0.03
instead of 0.99/0.01. Buy the cheap-but-near-certain side, hold to resolution,
collect the gap.

This is the closest thing to a "real edge" a small retail bot has on
Polymarket. It works because:
- Most retail traders chase live news, not stale near-resolved markets
- The dollar opportunity per market is small ($0.02 per share), so big
  funds ignore it
- The timing risk is real (you tie up capital until resolution)

Risks (all real, all material):
- Resolution disputes — rare but can flip a market
- Capital lock-up — you can't redeploy until the market resolves
- Adverse selection — if it's at 0.97 there might be a reason. Read the
  market terms before trading. v1 filters for time-to-resolution but does
  not auto-read terms; that's a TODO.

Parameters:
- min_edge_per_share: minimum cents of edge required (default 0.015)
- min_volume_24h: filter out illiquid markets (default $1000)
- max_hours_to_resolution: only trade markets resolving within this window
- min_book_depth_usd: minimum size at the target price (default $20)
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from core.models import Platform, Side, Signal, StrategyMode
from strategies.base import Strategy, StrategyContext


class ResolutionArbStrategy(Strategy):
    name = "polymarket_resolution_arb"

    def __init__(
        self,
        *,
        exchange,                    # PolymarketExchange
        mode: StrategyMode = StrategyMode.DISABLED,
        min_edge_per_share: Decimal = Decimal("0.015"),
        min_volume_24h: Decimal = Decimal("1000"),
        max_hours_to_resolution: float = 72.0,
        min_book_depth_usd: Decimal = Decimal("20"),
        target_size_usd: Decimal = Decimal("25"),
    ):
        super().__init__(mode=mode)
        self.exchange = exchange
        self.min_edge = min_edge_per_share
        self.min_volume_24h = min_volume_24h
        self.max_hours = max_hours_to_resolution
        self.min_depth_usd = min_book_depth_usd
        self.target_size_usd = target_size_usd

    async def tick(self, ctx: StrategyContext) -> list[Signal]:
        if not self.enabled:
            return []

        markets = await self.exchange.list_markets(
            active_only=True,
            min_volume_24h=self.min_volume_24h,
        )
        candidates = []
        skip_no_end = 0
        skip_past = 0
        skip_too_far = 0
        for m in markets:
            if m.end_date is None:
                skip_no_end += 1
                continue
            time_left = m.end_date - ctx.now
            if time_left <= timedelta(0):
                skip_past += 1
                continue
            if time_left > timedelta(hours=self.max_hours):
                skip_too_far += 1
                continue
            candidates.append(m)

        self.log.info("resolution_arb.candidates",
                      count=len(candidates),
                      total=len(markets),
                      no_end=skip_no_end,
                      past=skip_past,
                      too_far=skip_too_far)

        signals: list[Signal] = []
        for m in candidates:
            try:
                book = await self.exchange.get_order_book(m)
            except Exception:
                self.log.warning("resolution_arb.book_failed", market=m.market_id)
                continue
            if book.best_ask is None or book.best_bid is None:
                continue

            # Buy YES if asking <= 1 - min_edge. The implicit assumption is
            # that "near resolution + still active" tilts toward YES being
            # likely. In practice, you'd read each market's metadata to
            # determine which side is the favorite. For scaffold safety,
            # we ONLY emit signals when the ask is extreme (<= 0.05 or
            # >= 0.95) — those are markets the crowd has already priced as
            # near-certain, and we're capturing the last few cents.
            ask = book.best_ask
            bid = book.best_bid

            # Cheap "no-side" — if YES ask is very low, buy NO at ~1-bid
            # If YES ask <= 0.05, the implied NO mid is 0.95+. We could buy
            # YES if it's so cheap the upside dominates, but resolution arb
            # specifically targets the >= 0.95 case.
            if ask >= Decimal("0.95") and ask <= Decimal("1") - self.min_edge:
                edge = (Decimal("1") - ask)
                # require book depth at the ask
                ask_size_usd = book.asks[0].size * ask
                if ask_size_usd < self.min_depth_usd:
                    continue
                size_usd = min(self.target_size_usd, ask_size_usd)
                share_count = (size_usd / ask).quantize(Decimal("0.01"))
                signals.append(Signal(
                    strategy=self.name,
                    platform=Platform.POLYMARKET,
                    market_id=m.market_id,
                    token_id=m.token_id,
                    side=Side.BUY,
                    target_price=ask,
                    target_size_usd=size_usd,
                    confidence=float(min(Decimal(1), edge / Decimal("0.05"))),
                    reason=(
                        f"resolution_arb: ask={ask} bid={bid} edge={edge} "
                        f"hrs_left={(m.end_date - ctx.now).total_seconds()/3600:.1f}"
                    ),
                    metadata={
                        "share_count_target": str(share_count),
                        "end_date": m.end_date.isoformat() if m.end_date else None,
                    },
                ))
        self.log.info("resolution_arb.signals", count=len(signals))
        return signals
