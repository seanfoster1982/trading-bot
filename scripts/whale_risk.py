"""Whale risk scoring — analyzes holder distribution patterns to detect
distribution / bull-trap setups.

Pulls from Birdeye:
  - /defi/v3/token/holder (top 10 holders)
  - /defi/v3/holder-stats/single (overall distribution stats)
  - /defi/v3/token/exit-liquidity (how much exit liquidity exists)

Stores whale_risk_score (0-100) in whale_risk table.
Higher score = more likely distribution / bull trap.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import click
import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv()

BIRDEYE_BASE = "https://public-api.birdeye.so"
BIRDEYE_KEY = os.getenv("BIRDEYE_API_KEY")
DB_PATH = Path("data/memecoins.db")


@dataclass
class WhaleReport:
    address: str
    symbol: str
    whale_risk_score: float
    top_10_pct: float
    top_10_pct_delta_24h: float  # change vs last check
    holder_count: int
    holder_count_delta_24h: int
    exit_liquidity_usd: float
    flags: list[str]


def init_whale_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS whale_risk (
            address TEXT NOT NULL,
            symbol TEXT NOT NULL,
            whale_risk_score REAL NOT NULL,
            top_10_pct REAL NOT NULL,
            top_10_pct_delta_24h REAL,
            holder_count INTEGER,
            holder_count_delta_24h INTEGER,
            exit_liquidity_usd REAL,
            flags TEXT NOT NULL,
            checked_at INTEGER NOT NULL,
            PRIMARY KEY (address, checked_at)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_whale_recent
        ON whale_risk(address, checked_at)
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


async def fetch_token_holders(client: httpx.AsyncClient, address: str) -> tuple[list[dict], str | None]:
    headers = {
        "X-API-KEY": BIRDEYE_KEY,
        "x-chain": "solana",
        "accept": "application/json",
    }
    try:
        r = await client.get(
            f"{BIRDEYE_BASE}/defi/v3/token/holder",
            headers=headers,
            params={"address": address, "limit": 10, "offset": 0},
            timeout=15.0,
        )
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}"
        data = r.json()
        if not data.get("success"):
            return [], f"API error"
        return data.get("data", {}).get("items", []) or [], None
    except Exception as e:
        return [], f"Exception: {str(e)[:100]}"


async def fetch_holder_stats(client: httpx.AsyncClient, address: str) -> dict | None:
    headers = {
        "X-API-KEY": BIRDEYE_KEY,
        "x-chain": "solana",
        "accept": "application/json",
    }
    try:
        r = await client.get(
            f"{BIRDEYE_BASE}/defi/v3/holder-stats/single",
            headers=headers,
            params={"address": address},
            timeout=15.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("success"):
            return None
        return data.get("data")
    except Exception:
        return None


# Exit-liquidity endpoint is EVM-only on Birdeye. Using liquidity/mcap proxy instead.


async def fetch_token_overview(client: httpx.AsyncClient, address: str) -> dict | None:
    headers = {
        "X-API-KEY": BIRDEYE_KEY,
        "x-chain": "solana",
        "accept": "application/json",
    }
    try:
        r = await client.get(
            f"{BIRDEYE_BASE}/defi/token_overview",
            headers=headers,
            params={"address": address},
            timeout=15.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("success"):
            return None
        return data.get("data")
    except Exception:
        return None


def get_previous_report(address: str, hours_ago: int = 24) -> dict | None:
    """Get the most recent whale_risk report for this token from N hours ago or older."""
    conn = sqlite3.connect(DB_PATH)
    cutoff = int(time.time()) - (hours_ago * 3600)
    row = conn.execute("""
        SELECT top_10_pct, holder_count, checked_at
        FROM whale_risk
        WHERE address = ? AND checked_at <= ?
        ORDER BY checked_at DESC
        LIMIT 1
    """, (address, cutoff)).fetchone()
    conn.close()
    if not row:
        return None
    return {"top_10_pct": row[0], "holder_count": row[1], "checked_at": row[2]}


def compute_whale_risk_score(
    *,
    top_10_pct: float,
    top_10_pct_delta: float,  # negative = top holders selling
    holder_count: int,
    holder_count_delta: int,  # positive = new buyers
    exit_liquidity_usd: float,
    market_cap_usd: float,
) -> tuple[float, list[str]]:
    """Score 0-100 where higher = more distribution risk / bull trap.

    Logic:
    - High absolute top-10 concentration is bad on its own (whale-heavy supply)
    - Falling top-10 share is the distribution signal — whales are exiting
    - Falling top-10 share + rising holder count = textbook bull trap pattern
    - Low exit liquidity relative to market cap means whales can't get out cleanly,
      which often precedes panic dumps
    """
    score = 0.0
    flags = []

    # Concentration baseline
    if top_10_pct > 70:
        score += 25
        flags.append(f"EXTREME_CONCENTRATION: top 10 own {top_10_pct:.0f}%")
    elif top_10_pct > 50:
        score += 15
        flags.append(f"HIGH_CONCENTRATION: top 10 own {top_10_pct:.0f}%")
    elif top_10_pct > 35:
        score += 8
        flags.append(f"ELEVATED_CONCENTRATION: top 10 own {top_10_pct:.0f}%")

    # Distribution signal (most important)
    if top_10_pct_delta < -3:  # >3pp drop in top-10 share
        score += 35
        flags.append(f"ACTIVE_DISTRIBUTION: top 10 share dropped {abs(top_10_pct_delta):.1f}pp")
    elif top_10_pct_delta < -1:
        score += 20
        flags.append(f"MODERATE_DISTRIBUTION: top 10 share dropped {abs(top_10_pct_delta):.1f}pp")

    # Bull trap pattern: holders growing fast while top share drops
    if holder_count_delta > 0 and top_10_pct_delta < -1:
        growth_pct = (holder_count_delta / max(holder_count - holder_count_delta, 1)) * 100
        if growth_pct > 10:
            score += 25
            flags.append(f"BULL_TRAP_PATTERN: holders +{growth_pct:.0f}% while top 10 sells")

    # Exit liquidity check
    if market_cap_usd > 0 and exit_liquidity_usd > 0:
        exit_ratio = exit_liquidity_usd / market_cap_usd
        if exit_ratio < 0.02:  # less than 2% of mcap is exit-able
            score += 15
            flags.append(f"LOW_EXIT_LIQUIDITY: only {exit_ratio*100:.1f}% of mcap")

    return min(100.0, score), flags


async def check_whale_risk(
    client: httpx.AsyncClient, symbol: str, address: str
) -> WhaleReport | None:
    """Full whale risk analysis for one token."""
    stats_task = fetch_holder_stats(client, address)
    overview_task = fetch_token_overview(client, address)
    stats, overview = await asyncio.gather(stats_task, overview_task)

    if not stats and not overview:
        return None

    top_10_pct = 0.0
    holder_count = 0
    if stats:
        top_10_pct = to_float(stats.get("top_10_pct"))
        holder_count = to_int(stats.get("holder"))

    market_cap = 0.0
    liquidity = 0.0
    if overview:
        market_cap = to_float(overview.get("marketCap"))
        liquidity = to_float(overview.get("liquidity"))
        if not holder_count:
            holder_count = to_int(overview.get("holder"))

    prev = get_previous_report(address, hours_ago=24)
    if prev:
        top_10_pct_delta = top_10_pct - prev["top_10_pct"]
        holder_count_delta = holder_count - prev["holder_count"]
    else:
        top_10_pct_delta = 0.0
        holder_count_delta = 0

    score, flags = compute_whale_risk_score(
        top_10_pct=top_10_pct,
        top_10_pct_delta=top_10_pct_delta,
        holder_count=holder_count,
        holder_count_delta=holder_count_delta,
        exit_liquidity_usd=liquidity,
        market_cap_usd=market_cap,
    )

    return WhaleReport(
        address=address,
        symbol=symbol,
        whale_risk_score=score,
        top_10_pct=top_10_pct,
        top_10_pct_delta_24h=top_10_pct_delta,
        holder_count=holder_count,
        holder_count_delta_24h=holder_count_delta,
        exit_liquidity_usd=liquidity,
        flags=flags,
    )


def save_whale_report(report: WhaleReport) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO whale_risk
        (address, symbol, whale_risk_score, top_10_pct, top_10_pct_delta_24h,
         holder_count, holder_count_delta_24h, exit_liquidity_usd, flags, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        report.address, report.symbol, report.whale_risk_score,
        report.top_10_pct, report.top_10_pct_delta_24h,
        report.holder_count, report.holder_count_delta_24h,
        report.exit_liquidity_usd, json.dumps(report.flags), int(time.time())
    ))
    conn.commit()
    conn.close()


def get_tradeable_tokens() -> list[tuple[str, str]]:
    """Tokens that pass rug check (low rug score, no freeze authority)."""
    conn = sqlite3.connect(DB_PATH)
    cutoff = int(time.time()) - (24 * 3600)
    rows = conn.execute("""
        SELECT DISTINCT s.symbol, s.address
        FROM screened_tokens s
        INNER JOIN rug_reports r ON s.address = r.address
        WHERE s.screened_at >= ?
          AND r.rug_score < 25
          AND r.freeze_authority_active = 0
    """, (cutoff,)).fetchall()
    conn.close()
    return rows


async def main_async(only_symbol: str | None):
    init_whale_db()
    if not BIRDEYE_KEY:
        Console().print("[red]BIRDEYE_API_KEY not set in .env[/red]")
        return

    console = Console()
    console.print("[bold]Whale Risk Scoring[/bold]\n")

    targets = get_tradeable_tokens()
    if only_symbol:
        targets = [(s, a) for s, a in targets if s.upper() == only_symbol.upper()]

    if not targets:
        console.print("[yellow]No tradeable tokens found. Run screen + rug_check first.[/yellow]")
        return

    console.print(f"Analyzing {len(targets)} tokens for whale distribution patterns\n")

    reports: list[WhaleReport] = []
    sem = asyncio.Semaphore(4)

    async def analyze_one(sym, addr, client):
        async with sem:
            r = await check_whale_risk(client, sym, addr)
            if r:
                save_whale_report(r)
                reports.append(r)
            await asyncio.sleep(0.2)

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*[analyze_one(s, a, client) for s, a in targets])

    if not reports:
        console.print("[yellow]No reports generated.[/yellow]")
        return

    reports.sort(key=lambda r: r.whale_risk_score, reverse=True)

    table = Table(title="Whale Risk Analysis")
    table.add_column("Symbol")
    table.add_column("Risk", justify="right")
    table.add_column("Top 10%", justify="right")
    table.add_column("Δ Top 10%", justify="right")
    table.add_column("Holders", justify="right")
    table.add_column("Δ Holders", justify="right")
    table.add_column("Exit Liq", justify="right")
    table.add_column("Flags", overflow="fold", max_width=60)

    for r in reports:
        score_color = "red" if r.whale_risk_score >= 50 else ("yellow" if r.whale_risk_score >= 25 else "green")
        flags_str = "; ".join(r.flags) if r.flags else "[green]clean[/green]"
        table.add_row(
            r.symbol[:10],
            f"[{score_color}]{r.whale_risk_score:.0f}[/{score_color}]",
            f"{r.top_10_pct:.0f}%",
            f"{r.top_10_pct_delta_24h:+.1f}pp" if r.top_10_pct_delta_24h != 0 else "--",
            str(r.holder_count) if r.holder_count else "-",
            f"{r.holder_count_delta_24h:+d}" if r.holder_count_delta_24h != 0 else "--",
            f"${r.exit_liquidity_usd/1000:.0f}K" if r.exit_liquidity_usd > 0 else "-",
            flags_str,
        )
    console.print(table)

    safe = sum(1 for r in reports if r.whale_risk_score < 25)
    risky = sum(1 for r in reports if 25 <= r.whale_risk_score < 50)
    danger = sum(1 for r in reports if r.whale_risk_score >= 50)
    console.print(f"\n[green]Safe: {safe}[/green]  [yellow]Risky: {risky}[/yellow]  [red]Danger: {danger}[/red]")
    console.print("\nNext: macro regime scoring + Fear & Greed (next session)")


@click.command()
@click.option("--only-symbol", default=None, help="Only analyze one symbol.")
def main(only_symbol):
    asyncio.run(main_async(only_symbol))


if __name__ == "__main__":
    main()
