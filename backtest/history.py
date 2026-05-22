"""backtest/history.py — uses startTs/endTs to fetch real market lifetime history."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

CLOB_HOST_DEFAULT = "https://clob.polymarket.com"
GAMMA_HOST_DEFAULT = "https://gamma-api.polymarket.com"


@dataclass
class HistoricalMarket:
    market_id: str
    token_id: str
    question: str
    outcome: str
    resolved_to: int | None
    end_date: datetime
    history: list[tuple[datetime, Decimal]]

    @property
    def resolution_value(self) -> Decimal | None:
        if self.resolved_to is None:
            return None
        return Decimal(self.resolved_to)


class HistoryFetcher:
    def __init__(
        self,
        *,
        clob_host: str = CLOB_HOST_DEFAULT,
        gamma_host: str = GAMMA_HOST_DEFAULT,
        cache_dir: Path = Path("data/cache"),
        timeout: float = 20.0,
    ):
        self._clob_host = clob_host.rstrip("/")
        self._gamma_host = gamma_host.rstrip("/")
        self._cache = cache_dir
        self._cache.mkdir(parents=True, exist_ok=True)
        self._timeout = timeout

    async def list_resolved_markets(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(base_url=self._gamma_host, timeout=self._timeout) as c:
            r = await c.get("/markets", params={
                "closed": "true", "active": "false",
                "limit": limit, "offset": offset,
                "order": "endDate", "ascending": "false",
            })
            r.raise_for_status()
            return r.json()

    async def fetch_price_history_window(
        self,
        token_id: str,
        *,
        start_ts: int,
        end_ts: int,
        fidelity: int = 60,
    ) -> list[tuple[datetime, Decimal]]:
        """Query by absolute time window. fidelity is candle resolution in minutes.
        Polymarket's `interval` param means a trailing window from NOW, so for
        already-resolved markets it returns near-empty data. We use startTs/endTs
        to actually capture the market's trading history.
        """
        cache_path = self._cache / f"hist_{token_id}_{start_ts}_{end_ts}_{fidelity}.json"
        if cache_path.exists():
            with cache_path.open() as f:
                raw = json.load(f)
            return [(datetime.fromisoformat(ts), Decimal(p)) for ts, p in raw]

        async with httpx.AsyncClient(base_url=self._clob_host, timeout=self._timeout) as c:
            r = await c.get("/prices-history", params={
                "market": token_id,
                "startTs": start_ts,
                "endTs": end_ts,
                "fidelity": fidelity,
            })
            r.raise_for_status()
            payload = r.json()

        history_raw = payload.get("history", []) or []
        history: list[tuple[datetime, Decimal]] = []
        for pt in history_raw:
            ts = pt.get("t")
            p = pt.get("p")
            if ts is None or p is None:
                continue
            history.append((datetime.fromtimestamp(int(ts), tz=timezone.utc), Decimal(str(p))))

        with cache_path.open("w") as f:
            json.dump([(ts.isoformat(), str(p)) for ts, p in history], f)
        return history

    async def fetch_resolved_set(
        self, *,
        max_markets: int = 50,
        min_history_points: int = 10,
        fidelity: int = 60,
        lookback_days: int = 30,
    ) -> list[HistoricalMarket]:
        gamma_limit = max(max_markets * 5, 100)
        raw_markets = await self.list_resolved_markets(limit=gamma_limit)
        print(f"  [DIAG] Gamma returned {len(raw_markets)} raw markets (asked for {gamma_limit})")

        if raw_markets:
            sample = raw_markets[0]
            print(f"  [DIAG] sample question: {(sample.get('question') or '?')[:70]}")
            print(f"  [DIAG] sample endDate: {sample.get('endDate')!r}  closedTime: {sample.get('closedTime')!r}")
            print(f"  [DIAG] sample createdAt: {sample.get('createdAt')!r}")

        out: list[HistoricalMarket] = []
        sem = asyncio.Semaphore(5)
        rejected = {
            "no_tokens": 0,
            "no_resolution_date": 0,
            "fetch_error": 0,
            "too_short": 0,
        }
        sample_hist_sizes = []  # diagnostic — collect a few examples

        async def process(idx: int, rm: dict) -> HistoricalMarket | None:
            async with sem:
                token_ids = rm.get("clobTokenIds")
                if isinstance(token_ids, str):
                    try:
                        token_ids = json.loads(token_ids)
                    except Exception:
                        token_ids = None
                outcomes = rm.get("outcomes")
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except Exception:
                        outcomes = None
                if not token_ids or not outcomes:
                    rejected["no_tokens"] += 1
                    return None
                token_id = token_ids[0]
                outcome = outcomes[0]

                resolution_date_str = rm.get("closedTime") or rm.get("endDate")
                if not resolution_date_str:
                    rejected["no_resolution_date"] += 1
                    return None
                try:
                    resolution_dt = datetime.fromisoformat(
                        resolution_date_str.replace("Z", "+00:00")
                    )
                except Exception:
                    rejected["no_resolution_date"] += 1
                    return None

                outcome_prices = rm.get("outcomePrices")
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = json.loads(outcome_prices)
                    except Exception:
                        outcome_prices = None
                resolved_to: int | None = None
                if outcome_prices:
                    try:
                        resolved_to = 1 if float(outcome_prices[0]) >= 0.5 else 0
                    except Exception:
                        pass

                # Build time window: from createdAt (or N days before resolution)
                # to resolution time.
                created_str = rm.get("createdAt") or rm.get("startDate")
                start_dt = None
                if created_str:
                    try:
                        start_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    except Exception:
                        start_dt = None
                if start_dt is None:
                    start_dt = resolution_dt - timedelta(days=lookback_days)

                # Sanity: ensure start < end
                if start_dt >= resolution_dt:
                    start_dt = resolution_dt - timedelta(days=lookback_days)

                start_ts = int(start_dt.timestamp())
                end_ts = int(resolution_dt.timestamp())

                try:
                    hist = await self.fetch_price_history_window(
                        token_id, start_ts=start_ts, end_ts=end_ts, fidelity=fidelity,
                    )
                except Exception as e:
                    rejected["fetch_error"] += 1
                    if idx < 3:
                        print(f"  [DIAG] market {idx} fetch error: {e}"[:160])
                    return None

                if idx < 5:
                    sample_hist_sizes.append((idx, len(hist), resolution_date_str))

                if len(hist) < min_history_points:
                    rejected["too_short"] += 1
                    return None

                return HistoricalMarket(
                    market_id=str(rm.get("id") or rm.get("conditionId") or ""),
                    token_id=token_id,
                    question=rm.get("question", "?"),
                    outcome=outcome,
                    resolved_to=resolved_to,
                    end_date=resolution_dt,
                    history=hist,
                )

        results = await asyncio.gather(*[process(i, rm) for i, rm in enumerate(raw_markets)])
        out = [r for r in results if r is not None][:max_markets]
        print(f"  [DIAG] first 5 market history sizes: {sample_hist_sizes}")
        print(f"  [DIAG] kept {len(out)} markets. rejected: {rejected}")
        return out