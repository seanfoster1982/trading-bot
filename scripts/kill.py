"""
Kill switch.

Run this any time you need to stop trading immediately and flatten
positions. It connects directly to each exchange and calls cancel_all,
independent of whether the main engine is running.

Usage:
    python scripts/kill.py
    python scripts/kill.py --platform polymarket
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
import structlog

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_settings
from exchanges.polymarket import PolymarketExchange
from exchanges.solana import SolanaExchange


@click.command()
@click.option(
    "--platform",
    type=click.Choice(["all", "polymarket", "solana"]),
    default="all",
)
def main(platform: str):
    settings = get_settings()
    log = structlog.get_logger("kill")
    log.warning("kill.requested", platform=platform)
    asyncio.run(_run(settings, platform, log))


async def _run(settings, platform: str, log) -> None:
    cancelled_total = 0

    if platform in ("all", "polymarket"):
        try:
            poly = PolymarketExchange(
                host=settings.polymarket_host,
                gamma_host=settings.polymarket_gamma_host,
                chain_id=settings.polymarket_chain_id,
                private_key=settings.polymarket_private_key.get_secret_value(),
                funder_address=settings.polymarket_funder_address,
                signature_type=settings.polymarket_signature_type,
            )
            await poly.connect()
            n = await poly.cancel_all()
            cancelled_total += n
            log.warning("kill.polymarket_done", cancelled=n)
            await poly.close()
        except Exception:
            log.exception("kill.polymarket_failed")

    if platform in ("all", "solana"):
        # Solana swaps are atomic; nothing to cancel. Just log.
        log.warning("kill.solana_no_op",
                    reason="Solana swaps are atomic and cannot be cancelled")

    log.warning("kill.complete", total_cancelled=cancelled_total)


if __name__ == "__main__":
    main()
