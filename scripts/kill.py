"""
Kill / stop switch.

Polymarket: cancel open orders (no flatten beyond cancel_all).
Solana: no-op (atomic swaps).
Robinhood Chain: durable NEW-BUY halt via stop_control (no liquidation).

Usage:
    python scripts/kill.py
    python scripts/kill.py --platform robinhood
    python scripts/kill.py --platform polymarket
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
import structlog

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import get_settings
from exchanges.polymarket import PolymarketExchange
from exchanges.solana import SolanaExchange
import stop_control


@click.command()
@click.option(
    "--platform",
    type=click.Choice(["all", "polymarket", "solana", "robinhood"]),
    default="all",
)
def main(platform: str):
    settings = get_settings()
    log = structlog.get_logger("kill")
    log.warning("kill.requested", platform=platform)
    asyncio.run(_run(settings, platform, log))


async def _run(settings, platform: str, log) -> None:
    cancelled_total = 0

    if platform in ("all", "robinhood"):
        try:
            out = stop_control.request_halt(requested_by="kill.py", reason="KILL_SWITCH")
            log.warning(
                "kill.rh_halt",
                phase=out.phase,
                generation=out.generation,
                message=out.message,
            )
            if out.phase != "HALT_ACTIVE":
                log.error("kill.rh_halt_failed", message=out.message)
        except Exception:
            log.exception("kill.rh_halt_failed")

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
        log.warning(
            "kill.solana_no_op",
            reason="Solana swaps are atomic and cannot be cancelled",
        )

    log.warning(
        "kill.complete",
        total_cancelled=cancelled_total,
        rh_liquidation=False,
        note="RH halt is no-new-buy only",
    )


if __name__ == "__main__":
    main()
