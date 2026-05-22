"""
Jupiter aggregator client.

Jupiter is the de facto Solana swap router — aggregates Raydium, Orca,
Meteora, and dozens more, returns the best route. Phantom uses it under
the hood for in-wallet swaps.

Two-step swap: /quote → /swap. Quote returns route + expected output.
Swap returns a base64 transaction we sign with the wallet private key
and submit via RPC.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


class JupiterClient:
    def __init__(self, host: str, timeout: float = 10.0):
        self._host = host.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._host,
            timeout=timeout,
            headers={"User-Agent": "trading-bot/0.1"},
        )

    async def close(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def quote(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount_atomic: int,         # in smallest unit (lamports for SOL, etc.)
        slippage_bps: int = 50,     # 50 = 0.5%
    ) -> dict[str, Any]:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_atomic),
            "slippageBps": str(slippage_bps),
            "swapMode": "ExactIn",
        }
        r = await self._client.get("/quote", params=params)
        r.raise_for_status()
        return r.json()

    async def swap(
        self,
        *,
        quote_response: dict,
        user_public_key: str,
        wrap_unwrap_sol: bool = True,
        compute_unit_price_micro_lamports: int | None = None,
    ) -> str:
        """Returns base64 unsigned transaction. Caller signs + submits via RPC."""
        body = {
            "quoteResponse": quote_response,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": wrap_unwrap_sol,
        }
        if compute_unit_price_micro_lamports is not None:
            body["computeUnitPriceMicroLamports"] = compute_unit_price_micro_lamports
        r = await self._client.post("/swap", json=body)
        r.raise_for_status()
        data = r.json()
        return data["swapTransaction"]

    async def get_price_pair(
        self,
        *,
        input_mint: str,
        output_mint: str,
        probe_amount_atomic: int,
    ) -> Decimal | None:
        """Convenience: quote a small amount to read effective price."""
        try:
            q = await self.quote(
                input_mint=input_mint,
                output_mint=output_mint,
                amount_atomic=probe_amount_atomic,
            )
            in_amt = Decimal(q["inAmount"])
            out_amt = Decimal(q["outAmount"])
            if in_amt == 0:
                return None
            return out_amt / in_amt
        except Exception:
            log.exception("jupiter.price_probe_failed")
            return None
