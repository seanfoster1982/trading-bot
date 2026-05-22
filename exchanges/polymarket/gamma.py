"""
Gamma API client. Gamma is Polymarket's market metadata service — gives you
the universe of markets with questions, end dates, volume, liquidity, and
the all-important token IDs needed for the CLOB.

Public, no auth required for reads.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog

from core.models import Market, Platform

log = structlog.get_logger(__name__)


class GammaClient:
    def __init__(self, host: str, timeout: float = 10.0):
        self._host = host.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._host,
            timeout=timeout,
            headers={"User-Agent": "trading-bot/0.1"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ----- markets ---------------------------------------------------------

    async def list_markets(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        limit: int = 500,
        offset: int = 0,
        min_volume_24h: Decimal | None = None,
    ) -> list[Market]:
        params: dict[str, Any] = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
        }
        r = await self._client.get("/markets", params=params)
        r.raise_for_status()
        data = r.json()
        markets = [self._to_market(m) for m in data]
        if min_volume_24h is not None:
            markets = [
                m for m in markets
                if Decimal(str(m.metadata.get("volume_24h", 0))) >= min_volume_24h
            ]
        return [m for m in markets if m is not None]

    async def get_market(self, market_id: str) -> Market | None:
        r = await self._client.get(f"/markets/{market_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return self._to_market(r.json())

    # ----- helpers ---------------------------------------------------------

    @staticmethod
    def _to_market(raw: dict) -> Market | None:
        """Map Gamma response to our Market model. Gamma sometimes returns
        outcomes as a JSON-encoded string; handle both."""
        try:
            token_ids = raw.get("clobTokenIds")
            if isinstance(token_ids, str):
                import json
                token_ids = json.loads(token_ids)
            outcomes = raw.get("outcomes")
            if isinstance(outcomes, str):
                import json
                outcomes = json.loads(outcomes)

            # for now, take the YES side; strategies may flip
            primary_token = token_ids[0] if token_ids else None
            primary_outcome = outcomes[0] if outcomes else None

            end_str = raw.get("endDate")
            end_dt = (
                datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                if end_str
                else None
            )
            return Market(
                platform=Platform.POLYMARKET,
                market_id=str(raw.get("id") or raw.get("conditionId") or ""),
                token_id=primary_token,
                question=raw.get("question"),
                outcome=primary_outcome,
                end_date=end_dt,
                tick_size=Decimal(str(raw.get("tickSize", "0.01"))),
                min_size=Decimal(str(raw.get("minimumOrderSize", "5"))),
                metadata={
                    "condition_id": raw.get("conditionId"),
                    "volume_24h": raw.get("volume24hr") or raw.get("volume24Hr") or 0,
                    "volume_total": raw.get("volume") or 0,
                    "liquidity": raw.get("liquidity") or 0,
                    "neg_risk": raw.get("negRisk", False),
                    "all_token_ids": token_ids or [],
                    "all_outcomes": outcomes or [],
                    "slug": raw.get("slug"),
                },
            )
        except Exception as e:
            log.warning("gamma.parse_failed", error=str(e), raw_id=raw.get("id"))
            return None
