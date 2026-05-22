"""
Main run loop. Wire everything up and start the engine.

Usage:
    python scripts/run.py --strategies polymarket_resolution_arb,polymarket_copy_trade
    python scripts/run.py --strategies polymarket_resolution_arb --live
    python scripts/run.py --tick 60 --strategies polymarket_resolution_arb

LIVE flag requires LIVE_TRADING_ENABLED=true in .env AND the strategy
to be set to mode=live in code. Defense in depth.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from decimal import Decimal
from pathlib import Path

import click
import structlog

# Allow running from project root: `python scripts/run.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_settings
from core.engine import Engine
from core.models import Platform, StrategyMode
from data import get_persistence
from exchanges.polymarket import PolymarketExchange
from exchanges.solana import SolanaExchange
from risk.manager import RiskLimits, RiskManager
from strategies.polymarket.copy_trade import PolymarketCopyTradeStrategy
from strategies.polymarket.cross_platform_arb import CrossPlatformArbStrategy
from strategies.polymarket.news_event import NewsEventStrategy
from strategies.polymarket.resolution_arb import ResolutionArbStrategy
from strategies.polymarket.score_based import ScoreBasedStrategy
from strategies.solana.copy_trade import SolanaCopyTradeStrategy
from strategies.solana.jupiter_arb import JupiterArbStrategy


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stdout,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
    )


@click.command()
@click.option(
    "--strategies",
    default="polymarket_resolution_arb",
    help="Comma-separated list of strategy names to enable.",
)
@click.option("--mode", type=click.Choice(["paper", "live"]), default="paper",
              help="Paper trades simulate fills; live submits to exchange.")
@click.option("--tick", type=float, default=30.0, help="Seconds between ticks.")
def main(strategies: str, mode: str, tick: float):
    settings = get_settings()
    _configure_logging(settings.log_level)
    log = structlog.get_logger("run")

    if mode == "live" and not settings.live_trading_enabled:
        log.error("run.live_blocked",
                  reason="LIVE_TRADING_ENABLED=false in .env")
        sys.exit(2)

    enabled_names = {s.strip() for s in strategies.split(",") if s.strip()}
    log.info("run.start", strategies=list(enabled_names), mode=mode, tick=tick)

    asyncio.run(_run(settings, enabled_names, mode, tick))


async def _run(settings, enabled_names: set[str], mode: str, tick: float):
    log = structlog.get_logger("run")
    strategy_mode = StrategyMode.LIVE if mode == "live" else StrategyMode.PAPER

    # --- exchanges ----------------------------------------------------------
    poly = PolymarketExchange(
        host=settings.polymarket_host,
        gamma_host=settings.polymarket_gamma_host,
        chain_id=settings.polymarket_chain_id,
        private_key=settings.polymarket_private_key.get_secret_value(),
        funder_address=settings.polymarket_funder_address,
        signature_type=settings.polymarket_signature_type,
    )
    sol = SolanaExchange(
        rpc_url=settings.solana_rpc_url,
        jupiter_host=settings.jupiter_api_host,
        private_key_b58=settings.solana_private_key.get_secret_value(),
    )

    # Connect only what's needed by enabled strategies
    needs_poly = any(n.startswith("polymarket_") for n in enabled_names)
    needs_sol = any(n.startswith("solana_") for n in enabled_names)
    if needs_poly:
        try:
            await poly.connect()
        except Exception:
            log.exception("run.polymarket_connect_failed")
            return
    if needs_sol:
        try:
            await sol.connect()
        except Exception:
            log.exception("run.solana_connect_failed")
            return

    # --- strategies ---------------------------------------------------------
    all_strategies = [
        ResolutionArbStrategy(exchange=poly, mode=StrategyMode.DISABLED),
        PolymarketCopyTradeStrategy(
            exchange=poly,
            wallet_addresses=[],   # fill from config later
            mode=StrategyMode.DISABLED,
        ),
        CrossPlatformArbStrategy(
            polymarket_exchange=poly,
            event_map=[],
            mode=StrategyMode.DISABLED,
        ),
        NewsEventStrategy(
            polymarket_exchange=poly,
            anthropic_api_key=settings.anthropic_api_key.get_secret_value(),
            news_api_key=settings.news_api_key.get_secret_value(),
            mode=StrategyMode.DISABLED,
        ),
        ScoreBasedStrategy(
            exchange=poly,
            mode=StrategyMode.DISABLED,   # baseline benchmark only
        ),
        JupiterArbStrategy(
            solana_exchange=sol,
            triangular_paths=[],
            mode=StrategyMode.DISABLED,
        ),
        SolanaCopyTradeStrategy(
            solana_exchange=sol,
            wallet_addresses=[],
            mode=StrategyMode.DISABLED,
        ),
    ]

    for strat in all_strategies:
        if strat.name in enabled_names:
            strat.mode = strategy_mode

    active = [s for s in all_strategies if s.enabled]
    if not active:
        log.error("run.no_strategies_enabled", requested=list(enabled_names))
        return

    # --- risk ---------------------------------------------------------------
    risk = RiskManager(RiskLimits(
        max_position_usd=Decimal(str(settings.max_position_usd)),
        max_total_exposure_usd=Decimal(str(settings.max_total_exposure_usd)),
        max_daily_loss_usd=Decimal(str(settings.max_daily_loss_usd)),
        max_open_positions=settings.max_open_positions,
        kill_on_daily_loss=settings.kill_on_daily_loss,
    ))

    # --- persistence --------------------------------------------------------
    persistence = get_persistence(settings.database_url)
    log.info("run.persistence_ready", database_url=settings.database_url.split("@")[-1])

    # --- engine -------------------------------------------------------------
    engine = Engine(
        exchanges={Platform.POLYMARKET: poly, Platform.SOLANA: sol},
        strategies=active,
        risk=risk,
        tick_seconds=tick,
        live_global=settings.live_trading_enabled and mode == "live",
        persistence=persistence,
    )

    try:
        await engine.run()
    except KeyboardInterrupt:
        log.warning("run.interrupted")
    finally:
        try:
            await poly.close()
        except Exception:
            pass
        try:
            await sol.close()
        except Exception:
            pass
        try:
            persistence.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
