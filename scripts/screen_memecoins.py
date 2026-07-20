"""Memecoin screener — runs 3 parallel screens via Birdeye Token List V3.

Screen A (Momentum): mid-cap memecoins with real volume and liquidity
Screen B (Whale Targets): tokens with active Smart Money entries  
Screen C (Lottery): new low-cap tokens with basic safety filters

Outputs to SQLite (data/memecoins.db) — populates screened_tokens table.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import click
import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale_config import WHALE_ONLY, SCREEN_TOKEN_LIMIT, WHALE_TOP_TRADER_DEPTH  # noqa: E402

load_dotenv()

BIRDEYE_BASE = "https://public-api.birdeye.so"
BIRDEYE_KEY = os.getenv("BIRDEYE_API_KEY")
DB_PATH = Path("data/memecoins.db")

# Denylist: known non-memecoin assets we don't want to trade
# (wrapped majors, stablecoins, liquid staking, tokenized stocks, protocol tokens)
DENYLIST_SYMBOLS = {
    "cbBTC", "WBTC", "WETH", "WSOL", "USDC", "USDT", "USDS", "USDe",
    "JupUSD", "JupSOL", "jupSOL", "syrupUSDC", "CASH",
    "TSLAx", "CRCLx", "AAPLx", "NVDAx", "METAx", "MSFTx", "GOOGLx",
    "AMZNx", "COINx", "HOODx", "PLTRx", "GOOGx",
    "KMNO", "ZEC", "BNB", "ETH", "BTC",
    "mSOL", "bSOL", "INF", "LST", "jitoSOL",
}

# Screen configurations — tweak these as we learn what works
SCREEN_A_MOMENTUM = {
    "name": "momentum",
    "min_market_cap": 10_000_000,
    "max_market_cap": 150_000_000,
    "min_liquidity": 300_000,
    "min_volume_24h": 1_000_000,
    "min_holder": 2000,
    "min_age_hours": 14 * 24,
    "sort_by": "volume_24h_usd",
    "limit": 50,
}

SCREEN_B_WHALE = {
    "name": "whale_target",
    # Whale targets reuse Screen A filters; the whale signal comes from
    # querying Top Traders per token in a separate pass.
    "min_market_cap": 5_000_000,
    "max_market_cap": 200_000_000,
    "min_liquidity": 200_000,
    "min_volume_24h": 500_000,
    "min_holder": 1000,
    "min_age_hours": 7 * 24,
    "sort_by": "volume_24h_usd",
    "limit": 50,
}

SCREEN_C_LOTTERY = {
    "name": "lottery",
    "min_market_cap": 500_000,
    "max_market_cap": 5_000_000,
    "min_liquidity": 100_000,
    "min_volume_24h": 50_000,
    "min_holder": 500,
    "min_age_hours": 24,
    "max_age_hours": 7 * 24,
    "sort_by": "volume_24h_usd",
    "limit": 20,
}


# Additional sort variants - same thresholds but sorted differently.
# Running all 3 sorts per screen catches tokens that aren't top-by-volume.
# Results are deduped by address in main_async.

SCREEN_A_BY_LIQUIDITY = dict(SCREEN_A_MOMENTUM, sort_by="liquidity")
SCREEN_A_BY_MCAP = dict(SCREEN_A_MOMENTUM, sort_by="market_cap")

SCREEN_B_BY_LIQUIDITY = dict(SCREEN_B_WHALE, sort_by="liquidity")
SCREEN_B_BY_MCAP = dict(SCREEN_B_WHALE, sort_by="market_cap")

SCREEN_C_BY_LIQUIDITY = dict(SCREEN_C_LOTTERY, sort_by="liquidity")
SCREEN_C_BY_24H_CHANGE = dict(SCREEN_C_LOTTERY, sort_by="price_change_24h_percent")



@dataclass
class ScreenedToken:
    symbol: str
    address: str
    market_cap: float
    liquidity: float
    volume_24h: float
    price: float
    price_change_24h: float
    holder_count: int
    listed_at: int | None
    screen: str = ""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS screened_tokens (
            screen TEXT NOT NULL,
            symbol TEXT NOT NULL,
            address TEXT NOT NULL,
            market_cap REAL,
            liquidity REAL,
            volume_24h REAL,
            price REAL,
            price_change_24h REAL,
            holder_count INTEGER,
            listed_at INTEGER,
            screened_at INTEGER NOT NULL,
            PRIMARY KEY (screen, address, screened_at)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_screened_recent
        ON screened_tokens(screen, screened_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS whale_signals (
            address TEXT NOT NULL,
            symbol TEXT,
            trader_wallet TEXT NOT NULL,
            trade_volume_usd REAL,
            seen_at INTEGER NOT NULL,
            PRIMARY KEY (address, trader_wallet, seen_at)
        )
    """)
    conn.commit()
    conn.close()


def to_float(v, default=0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def to_int(v, default=0) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def is_non_memecoin_by_pattern(symbol: str, holder_count: int = 0) -> tuple[bool, str]:
    """Heuristic check: does this token look like a stablecoin, wrapped asset,
    or tokenized stock based on symbol patterns and holder profile?
    Returns (is_non_memecoin, reason).
    """
    if not symbol or symbol == "?":
        return False, ""

    s = symbol.upper()

    # Pattern: ends in lowercase 'x' is the Solana convention for tokenized stocks
    # (TSLAx, MSTRx, CRCLx, etc.) ? symbol case is preserved from API
    if symbol.endswith("x") and len(symbol) >= 4 and symbol[:-1].isupper():
        return True, f"tokenized stock pattern (ends in 'x')"

    # Stablecoin patterns
    stable_markers = ["USD", "EUR", "GBP", "JPY", "DAI"]
    for marker in stable_markers:
        if marker in s and len(s) <= 8:
            # Allow real memecoins that happen to contain these substrings unlikely
            # but possible ? keep length check tight
            return True, f"stablecoin pattern (contains '{marker}')"

    # Wrapped/staked patterns (st-, w-, cb-, j-, m-, b- prefixes on common bases)
    wrapped_prefixes = ["ST", "W", "CB", "J", "M", "B", "H"]
    wrapped_bases = ["SOL", "BTC", "ETH", "USD", "USDT", "USDC"]
    for prefix in wrapped_prefixes:
        for base in wrapped_bases:
            if s == prefix + base or s == prefix.lower() + base:
                return True, f"wrapped/staked pattern ({prefix}+{base})"

    return False, ""


async def fetch_token_list_v3(
    client: httpx.AsyncClient,
    config: dict,
) -> tuple[list[ScreenedToken], str | None]:
    """Query Birdeye Token List V3 with the screen filters."""
    headers = {
        "X-API-KEY": BIRDEYE_KEY,
        "x-chain": "solana",
        "accept": "application/json",
    }
    now_ts = int(time.time())
    min_listing = None
    max_listing = None
    if "max_age_hours" in config:
        min_listing = now_ts - (config["max_age_hours"] * 3600)
    if "min_age_hours" in config:
        max_listing = now_ts - (config["min_age_hours"] * 3600)

    # Birdeye caps filters at 5 per request. Send the 5 most selective ones,
    # apply the rest client-side after results come back.
    params = {
        "sort_by": config.get("sort_by", "volume_24h_usd"),
        "sort_type": "desc",
        "min_liquidity": config.get("min_liquidity", 0),
        "min_volume_24h_usd": config.get("min_volume_24h", 0),
        "min_market_cap": config.get("min_market_cap", 0),
        "min_holder": config.get("min_holder", 0),
        "limit": config.get("limit", 50),
        "offset": 0,
    }
    # Bump limit so we have room to filter client-side without running short
    params["limit"] = max(params["limit"], 50)

    try:
        r = await client.get(
            f"{BIRDEYE_BASE}/defi/v3/token/list",
            headers=headers,
            params=params,
            timeout=30.0,
        )
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}: {r.text[:300]}"
        data = r.json()
        if not data.get("success"):
            return [], f"API error: {data.get('message', 'unknown')}"
        items = data.get("data", {}).get("items", []) or []
        tokens = []
        for it in items:
            mcap = to_float(it.get("market_cap"))
            # Client-side filters that we couldn't fit into the 5-filter API limit
            if "max_market_cap" in config and mcap > config["max_market_cap"]:
                continue
            listed_at_val = to_int(it.get("recent_listing_time")) or None
            if min_listing is not None and listed_at_val is not None:
                if listed_at_val < min_listing:
                    continue
            if max_listing is not None and listed_at_val is not None:
                if listed_at_val > max_listing:
                    continue
            symbol_check = str(it.get("symbol") or "")
            if symbol_check in DENYLIST_SYMBOLS:
                continue
            # Heuristic category filter ? catches non-memecoins we haven't denylisted
            holder_n = to_int(it.get("holder"))
            is_non_meme, reason = is_non_memecoin_by_pattern(symbol_check, holder_n)
            if is_non_meme:
                continue
            tokens.append(ScreenedToken(
                symbol=str(it.get("symbol") or "?"),
                address=str(it.get("address") or ""),
                market_cap=mcap,
                liquidity=to_float(it.get("liquidity")),
                volume_24h=to_float(it.get("volume_24h_usd")),
                price=to_float(it.get("price")),
                price_change_24h=to_float(it.get("price_change_24h_percent")),
                holder_count=to_int(it.get("holder")),
                listed_at=to_int(it.get("recent_listing_time")) or None,
                screen=config["name"],
            ))
        return tokens, None
    except Exception as e:
        return [], f"Exception: {str(e)[:300]}"


async def fetch_top_traders(
    client: httpx.AsyncClient,
    token_address: str,
    *,
    time_frame: str = "24h",
    limit: int = 10,
) -> list[dict]:
    """Get top traders for a token via Birdeye's top_traders endpoint."""
    headers = {
        "X-API-KEY": BIRDEYE_KEY,
        "x-chain": "solana",
        "accept": "application/json",
    }
    params = {
        "address": token_address,
        "time_frame": time_frame,
        "sort_by": "volume",
        "sort_type": "desc",
        "limit": limit,
        "offset": 0,
    }
    try:
        r = await client.get(
            f"{BIRDEYE_BASE}/defi/v2/tokens/top_traders",
            headers=headers,
            params=params,
            timeout=20.0,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        if not data.get("success"):
            return []
        return data.get("data", {}).get("items", []) or []
    except Exception:
        return []


def save_screened(tokens: list[ScreenedToken], screen_name: str) -> int:
    if not tokens:
        return 0
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    ts = int(time.time())
    inserted = 0
    for t in tokens:
        try:
            cur.execute("""
                INSERT OR REPLACE INTO screened_tokens
                (screen, symbol, address, market_cap, liquidity, volume_24h,
                 price, price_change_24h, holder_count, listed_at, screened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (screen_name, t.symbol, t.address, t.market_cap, t.liquidity,
                  t.volume_24h, t.price, t.price_change_24h, t.holder_count,
                  t.listed_at, ts))
            inserted += 1
        except Exception:
            continue
    conn.commit()
    conn.close()
    return inserted


def save_whale_signals(signals: list[tuple[str, str, str, float]]) -> int:
    """signals: list of (token_address, symbol, trader_wallet, trade_volume_usd)"""
    if not signals:
        return 0
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    ts = int(time.time())
    inserted = 0
    for addr, sym, wallet, vol in signals:
        try:
            cur.execute("""
                INSERT OR REPLACE INTO whale_signals
                (address, symbol, trader_wallet, trade_volume_usd, seen_at)
                VALUES (?, ?, ?, ?, ?)
            """, (addr, sym, wallet, vol, ts))
            inserted += 1
        except Exception:
            continue
    conn.commit()
    conn.close()
    return inserted


def print_screen_table(console: Console, name: str, tokens: list[ScreenedToken]) -> None:
    if not tokens:
        console.print(f"  [yellow]No tokens passed {name} filters.[/yellow]")
        return
    table = Table(title=f"Screen {name}: {len(tokens)} tokens")
    table.add_column("Symbol")
    table.add_column("Market Cap", justify="right")
    table.add_column("Liquidity", justify="right")
    table.add_column("24h Volume", justify="right")
    table.add_column("24h Change", justify="right")
    table.add_column("Holders", justify="right")
    table.add_column("Address")
    for t in tokens[:20]:
        chg_color = "green" if t.price_change_24h >= 0 else "red"
        table.add_row(
            t.symbol[:12],
            f"${t.market_cap/1e6:,.1f}M",
            f"${t.liquidity/1e3:,.0f}K",
            f"${t.volume_24h/1e3:,.0f}K",
            f"[{chg_color}]{t.price_change_24h:+.1f}%[/{chg_color}]",
            str(t.holder_count),
            t.address[:8] + "..." + t.address[-6:],
        )
    console.print(table)


async def main_async(*, run_whale_pass: bool, top_n_for_whales: int, whale_only: bool):
    init_db()
    if not BIRDEYE_KEY:
        Console().print("[red]BIRDEYE_API_KEY not set in .env[/red]")
        return

    console = Console()
    if whale_only:
        console.print("[bold]Whale Copy Screener — Screen B only[/bold]\n")
    else:
        console.print("[bold]Memecoin Screener — running 3 screens[/bold]\n")

    async with httpx.AsyncClient() as client:
        b_tokens: list[ScreenedToken] = []

        if not whale_only:
            # Screen A: Momentum candidates
            console.print("[cyan]Screen A — Momentum candidates...[/cyan]")
            a_tokens_all = []
            a_err = None
            for cfg in [SCREEN_A_MOMENTUM, SCREEN_A_BY_LIQUIDITY, SCREEN_A_BY_MCAP]:
                toks, err = await fetch_token_list_v3(client, cfg)
                if err:
                    a_err = err
                a_tokens_all.extend(toks or [])
            seen = set()
            a_tokens = []
            for t in a_tokens_all:
                if t.address not in seen:
                    seen.add(t.address)
                    a_tokens.append(t)
            if a_err:
                console.print(f"  [red]ERROR: {a_err}[/red]")
            else:
                saved = save_screened(a_tokens, "momentum")
                console.print(f"  Found {len(a_tokens)} tokens, saved {saved}")
                print_screen_table(console, "A (Momentum)", a_tokens)
            await asyncio.sleep(0.5)

        # Screen B: Whale target candidates
        console.print("\n[cyan]Screen B — Whale target candidates...[/cyan]" if not whale_only
                      else "[cyan]Screen B — Whale target candidates...[/cyan]")
        # Run Screen B with 3 sorts and merge
        b_configs = [SCREEN_B_WHALE, SCREEN_B_BY_LIQUIDITY, SCREEN_B_BY_MCAP]
        if whale_only:
            b_configs = [dict(c, limit=SCREEN_TOKEN_LIMIT) for c in b_configs]
        b_tokens_all = []
        b_err = None
        for cfg in b_configs:
            toks, err = await fetch_token_list_v3(client, cfg)
            if err:
                b_err = err
            b_tokens_all.extend(toks or [])
        seen = set()
        b_tokens = []
        for t in b_tokens_all:
            if t.address not in seen:
                seen.add(t.address)
                b_tokens.append(t)
        if b_err:
            console.print(f"  [red]ERROR: {b_err}[/red]")
        else:
            saved = save_screened(b_tokens, "whale_target")
            console.print(f"  Found {len(b_tokens)} tokens, saved {saved}")
            print_screen_table(console, "B (Whale Targets)", b_tokens)

        await asyncio.sleep(0.5)

        # Screen C: Lottery candidates (skipped in whale-only mode)
        if not whale_only:
            console.print("\n[cyan]Screen C — Lottery (new low-cap)...[/cyan]")
            c_tokens_all = []
            c_err = None
            for cfg in [SCREEN_C_LOTTERY, SCREEN_C_BY_LIQUIDITY, SCREEN_C_BY_24H_CHANGE]:
                toks, err = await fetch_token_list_v3(client, cfg)
                if err:
                    c_err = err
                c_tokens_all.extend(toks or [])
            seen = set()
            c_tokens = []
            for t in c_tokens_all:
                if t.address not in seen:
                    seen.add(t.address)
                    c_tokens.append(t)
            if c_err:
                console.print(f"  [red]ERROR: {c_err}[/red]")
            else:
                saved = save_screened(c_tokens, "lottery")
                console.print(f"  Found {len(c_tokens)} tokens, saved {saved}")
                print_screen_table(console, "C (Lottery)", c_tokens)

        # Whale signal pass — check Top Traders for top N Screen B tokens
        if run_whale_pass and b_tokens:
            console.print(f"\n[cyan]Whale pass — checking Top Traders for top {top_n_for_whales} Screen B tokens...[/cyan]")
            whale_signals = []
            for t in b_tokens[:top_n_for_whales]:
                traders = await fetch_top_traders(client, t.address)
                if not traders:
                    continue
                # Capture top 3 traders by volume per token — these are the wallets we'd potentially mirror
                for tr in traders[:3]:
                    wallet = tr.get("owner") or tr.get("address") or ""
                    vol = to_float(tr.get("volume"))
                    if wallet and vol > 0:
                        whale_signals.append((t.address, t.symbol, wallet, vol))
                await asyncio.sleep(0.2)  # gentle throttle
            saved = save_whale_signals(whale_signals)
            console.print(f"  Captured {len(whale_signals)} whale signals, saved {saved}")

    console.print("\n[green]Done.[/green] Check data/memecoins.db for full results.")
    console.print("Next: run [yellow]ingest_memecoins.py[/yellow] to pull OHLCV for screened tokens (next script).")


@click.command()
@click.option("--skip-whales", is_flag=True, help="Skip the Top Traders pass (saves CUs).")
@click.option("--whale-depth", type=int, default=None,
              help="How many Screen B tokens to check Top Traders for.")
@click.option("--whale-only", is_flag=True, default=None,
              help="Run Screen B only (whale copy product mode).")
def main(skip_whales, whale_depth, whale_only):
    depth = whale_depth if whale_depth is not None else WHALE_TOP_TRADER_DEPTH
    only = whale_only if whale_only is not None else WHALE_ONLY
    asyncio.run(main_async(
        run_whale_pass=not skip_whales,
        top_n_for_whales=depth,
        whale_only=only,
    ))


if __name__ == "__main__":
    main()
