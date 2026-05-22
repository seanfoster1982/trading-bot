"""
Scan markets. Read-only inspection of the Polymarket universe.

Usage:
    python scripts/scan_markets.py
    python scripts/scan_markets.py --min-volume 5000 --limit 20
    python scripts/scan_markets.py --resolution-window 48
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_settings
from exchanges.polymarket import GammaClient


@click.command()
@click.option("--min-volume", type=float, default=1000.0,
              help="Minimum 24h volume in USD.")
@click.option("--resolution-window", type=float, default=720.0,
              help="Only show markets resolving within N hours (default 720 = 30 days).")
@click.option("--limit", type=int, default=25)
def main(min_volume: float, resolution_window: float, limit: int):
    asyncio.run(_run(min_volume, resolution_window, limit))


async def _run(min_volume: float, resolution_window: float, limit: int):
    settings = get_settings()
    console = Console()
    cutoff = datetime.now(timezone.utc) + timedelta(hours=resolution_window)

    async with GammaClient(settings.polymarket_gamma_host) as g:
        markets = await g.list_markets(
            active=True, limit=500,
            min_volume_24h=Decimal(str(min_volume)),
        )

    upcoming = [
        m for m in markets
        if m.end_date is not None
        and m.end_date.replace(tzinfo=timezone.utc) <= cutoff
        and m.end_date.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
    ]
    upcoming.sort(key=lambda m: m.end_date)

    table = Table(title=f"Polymarket — resolving within {resolution_window}h, vol≥${min_volume:.0f}")
    table.add_column("Question", overflow="fold", max_width=60)
    table.add_column("Outcome")
    table.add_column("End", justify="right")
    table.add_column("Vol 24h", justify="right")
    table.add_column("Liq", justify="right")
    for m in upcoming[:limit]:
        v24 = float(m.metadata.get("volume_24h", 0) or 0)
        liq = float(m.metadata.get("liquidity", 0) or 0)
        end_str = m.end_date.strftime("%Y-%m-%d %H:%M") if m.end_date else "?"
        table.add_row(
            (m.question or "?")[:120],
            m.outcome or "?",
            end_str,
            f"${v24:,.0f}",
            f"${liq:,.0f}",
        )
    console.print(table)
    console.print(f"\n{len(upcoming)} markets matched, showing top {min(limit, len(upcoming))}")


if __name__ == "__main__":
    main()
