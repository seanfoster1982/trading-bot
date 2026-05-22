"""
Solana DEX arbitrage via Jupiter quote routing.

Edge: triangular price discrepancies between routes Jupiter aggregates.
Largely captured by MEV bots already, but pockets exist on smaller pairs
or moments of high traffic when MEV bots are saturated.

This is the kind of strategy where you make $0.30 per cycle and need
hundreds per day to be worth running, while a single failed transaction
costs you a few dollars in priority fees. Math has to work or it just
bleeds you.

v1 stub: scaffolds the structure. Real implementation needs:
- A list of triangular paths (USDC → SOL → BONK → USDC, etc.)
- Quote each path simultaneously
- Compute expected output net of fees + priority fees
- Submit only when edge > realistic execution cost
- MEV protection (Jito bundles or private mempool)
"""
from __future__ import annotations

from decimal import Decimal

from core.models import Signal, StrategyMode
from strategies.base import Strategy, StrategyContext


class JupiterArbStrategy(Strategy):
    name = "solana_jupiter_arb"

    def __init__(
        self,
        *,
        solana_exchange,
        triangular_paths: list[list[str]],   # e.g. [["USDC","SOL","BONK","USDC"]]
        mode: StrategyMode = StrategyMode.DISABLED,
        per_trade_usd: Decimal = Decimal("50"),
        min_net_edge_bps: int = 30,          # 30bps = 0.30% after fees
    ):
        super().__init__(mode=mode)
        self.exchange = solana_exchange
        self.paths = triangular_paths
        self.per_trade_usd = per_trade_usd
        self.min_edge_bps = min_net_edge_bps

    async def tick(self, ctx: StrategyContext) -> list[Signal]:
        if not self.enabled:
            return []
        self.log.info("jupiter_arb.stub", path_count=len(self.paths))
        return []
