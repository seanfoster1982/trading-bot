"""Rug check module — uses Birdeye token_security to filter scam tokens.

Adds rug_score (0-100) and rug_flags (list of issues) to screened tokens.
Tokens with active mint authority, freeze authority, or excessive holder
concentration get high scores and are flagged for the strategy to skip.

This is the Phase 1 rug check. RugCheck.xyz JWT integration is a Phase 1.5
enhancement we'll add after the bot is paper-trading.
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
class RugReport:
    address: str
    rug_score: float  # 0-100, higher = more dangerous
    flags: list[str]  # specific issues found
    mint_authority_active: bool
    freeze_authority_active: bool
    transfer_fee_bps: int  # basis points (100 = 1%)
    top_10_holder_pct: float  # 0-100
    mutable_metadata: bool


def init_rug_db() -> None:
    """Add rug_reports table to existing memecoins.db."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rug_reports (
            address TEXT PRIMARY KEY,
            symbol TEXT,
            rug_score REAL NOT NULL,
            flags TEXT NOT NULL,
            mint_authority_active INTEGER NOT NULL,
            freeze_authority_active INTEGER NOT NULL,
            transfer_fee_bps INTEGER NOT NULL,
            top_10_holder_pct REAL NOT NULL,
            mutable_metadata INTEGER NOT NULL,
            checked_at INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_rug_checked
        ON rug_reports(checked_at)
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


def to_bool(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).lower()
    return s in ("true", "1", "yes")


async def fetch_token_security(
    client: httpx.AsyncClient, address: str
) -> tuple[dict | None, str | None]:
    """Pull /defi/token_security from Birdeye for a token."""
    headers = {
        "X-API-KEY": BIRDEYE_KEY,
        "x-chain": "solana",
        "accept": "application/json",
    }
    try:
        r = await client.get(
            f"{BIRDEYE_BASE}/defi/token_security",
            headers=headers,
            params={"address": address},
            timeout=15.0,
        )
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        if not data.get("success"):
            return None, f"API error: {data.get('message', 'unknown')}"
        return data.get("data") or {}, None
    except Exception as e:
        return None, f"Exception: {str(e)[:200]}"


async def fetch_token_overview(
    client: httpx.AsyncClient, address: str
) -> dict | None:
    """Pull /defi/token_overview for holder concentration data."""
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
        return data.get("data") or {}
    except Exception:
        return None


async def fetch_token_holders(
    client: httpx.AsyncClient, address: str
) -> list[dict]:
    """Pull /defi/v3/token/holder for holder distribution analysis."""
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
            return []
        data = r.json()
        if not data.get("success"):
            return []
        return data.get("data", {}).get("items", []) or []
    except Exception:
        return []


def compute_rug_score(
    *,
    mint_authority_active: bool,
    freeze_authority_active: bool,
    transfer_fee_bps: int,
    top_10_holder_pct: float,
    mutable_metadata: bool,
) -> tuple[float, list[str]]:
    """Score 0-100. Higher = more dangerous.
    
    Critical issues (each adds 30+):
    - Mint authority still active (dev can mint unlimited supply)
    - Freeze authority still active (dev can freeze your wallet)
    - Transfer fees >5% (honeypot-style)
    
    Moderate issues (each adds 10-20):
    - Top 10 holders own >50% (concentration risk)
    - Mutable metadata (dev can change token name/image after launch)
    - Transfer fees 1-5% (tax-style, not a rug but eats returns)
    
    Minor issues (each adds 5-10):
    - Top 10 holders own 35-50% (high but not extreme)
    """
    score = 0.0
    flags = []

    if mint_authority_active:
        score += 40
        flags.append("MINT_AUTHORITY_ACTIVE: dev can mint unlimited supply")

    if freeze_authority_active:
        score += 35
        flags.append("FREEZE_AUTHORITY_ACTIVE: dev can freeze wallets")

    if transfer_fee_bps > 500:  # >5%
        score += 30
        flags.append(f"HIGH_TRANSFER_FEE: {transfer_fee_bps/100:.1f}% (honeypot-style)")
    elif transfer_fee_bps > 100:  # 1-5%
        score += 15
        flags.append(f"MODERATE_TRANSFER_FEE: {transfer_fee_bps/100:.1f}%")

    if top_10_holder_pct > 70:
        score += 30
        flags.append(f"EXTREME_CONCENTRATION: top 10 own {top_10_holder_pct:.0f}%")
    elif top_10_holder_pct > 50:
        score += 20
        flags.append(f"HIGH_CONCENTRATION: top 10 own {top_10_holder_pct:.0f}%")
    elif top_10_holder_pct > 35:
        score += 10
        flags.append(f"ELEVATED_CONCENTRATION: top 10 own {top_10_holder_pct:.0f}%")

    if mutable_metadata:
        score += 10
        flags.append("MUTABLE_METADATA: dev can change token name/image")

    return min(100.0, score), flags


async def check_token(
    client: httpx.AsyncClient, address: str, symbol: str = "?"
) -> RugReport | None:
    """Run all checks on a single token and produce a RugReport."""
    security, err = await fetch_token_security(client, address)
    if err or security is None:
        return None

    # Birdeye returns these fields; if missing, conservatively assume they're active
    mint_active = security.get("mintAuthority") is not None and security.get("mintAuthority") != ""
    freeze_active = security.get("freezeAuthority") is not None and security.get("freezeAuthority") != ""
    transfer_fee_pct = to_float(security.get("transferFeeData", {}).get("transfer_fee_basis_points") if isinstance(security.get("transferFeeData"), dict) else 0)
    transfer_fee_bps = int(transfer_fee_pct)
    mutable_metadata = to_bool(security.get("mutableMetadata"))

    # For top-10 holder concentration, pull holder list
    holders = await fetch_token_holders(client, address)
    top_10_pct = 0.0
    if holders:
        # Each holder dict has "ui_amount" and we need to get total supply too
        # The overview endpoint has totalSupply
        overview = await fetch_token_overview(client, address)
        total_supply = to_float(overview.get("supply")) if overview else 0
        if total_supply > 0:
            top_10_held = sum(to_float(h.get("ui_amount")) for h in holders[:10])
            top_10_pct = (top_10_held / total_supply) * 100

    score, flags = compute_rug_score(
        mint_authority_active=mint_active,
        freeze_authority_active=freeze_active,
        transfer_fee_bps=transfer_fee_bps,
        top_10_holder_pct=top_10_pct,
        mutable_metadata=mutable_metadata,
    )

    return RugReport(
        address=address,
        rug_score=score,
        flags=flags,
        mint_authority_active=mint_active,
        freeze_authority_active=freeze_active,
        transfer_fee_bps=transfer_fee_bps,
        top_10_holder_pct=top_10_pct,
        mutable_metadata=mutable_metadata,
    )


def save_rug_report(symbol: str, report: RugReport) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO rug_reports
        (address, symbol, rug_score, flags, mint_authority_active,
         freeze_authority_active, transfer_fee_bps, top_10_holder_pct,
         mutable_metadata, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        report.address, symbol, report.rug_score, json.dumps(report.flags),
        int(report.mint_authority_active), int(report.freeze_authority_active),
        report.transfer_fee_bps, report.top_10_holder_pct,
        int(report.mutable_metadata), int(time.time()),
    ))
    conn.commit()
    conn.close()


def get_recently_screened_tokens(max_age_hours: int = 24) -> list[tuple[str, str]]:
    """Get unique (symbol, address) pairs screened within last N hours."""
    conn = sqlite3.connect(DB_PATH)
    cutoff = int(time.time()) - (max_age_hours * 3600)
    rows = conn.execute("""
        SELECT DISTINCT symbol, address
        FROM screened_tokens
        WHERE screened_at >= ?
    """, (cutoff,)).fetchall()
    conn.close()
    return rows


async def main_async(*, recheck_all: bool, limit: int):
    init_rug_db()
    if not BIRDEYE_KEY:
        Console().print("[red]BIRDEYE_API_KEY not set in .env[/red]")
        return

    console = Console()
    console.print("[bold]Rug Check — analyzing screened tokens[/bold]\n")

    # Get tokens to check
    candidates = get_recently_screened_tokens(max_age_hours=24)
    if limit:
        candidates = candidates[:limit]

    if not candidates:
        console.print("[yellow]No screened tokens to check. Run screen_memecoins.py first.[/yellow]")
        return

    # Skip already-checked unless --recheck
    if not recheck_all:
        conn = sqlite3.connect(DB_PATH)
        already = set(row[0] for row in conn.execute("SELECT address FROM rug_reports").fetchall())
        conn.close()
        new_candidates = [(s, a) for s, a in candidates if a not in already]
        console.print(f"  {len(candidates)} screened tokens, {len(already)} already checked, {len(new_candidates)} new")
        candidates = new_candidates

    if not candidates:
        console.print("[green]All screened tokens already have rug reports. Use --recheck-all to refresh.[/green]")
        return

    # Check each
    reports: list[tuple[str, RugReport]] = []
    sem = asyncio.Semaphore(5)

    async def check_one(symbol: str, address: str):
        async with sem:
            report = await check_token(httpx_client, address, symbol)
            if report:
                save_rug_report(symbol, report)
                reports.append((symbol, report))
            await asyncio.sleep(0.2)  # gentle throttle

    async with httpx.AsyncClient() as httpx_client:
        console.print(f"Checking {len(candidates)} tokens...\n")
        await asyncio.gather(*[check_one(s, a) for s, a in candidates])

    # Display results
    if not reports:
        console.print("[yellow]No reports generated.[/yellow]")
        return

    reports.sort(key=lambda x: x[1].rug_score, reverse=True)

    table = Table(title="Rug Check Results")
    table.add_column("Symbol")
    table.add_column("Score", justify="right")
    table.add_column("Mint", justify="center")
    table.add_column("Freeze", justify="center")
    table.add_column("Fee%", justify="right")
    table.add_column("Top10%", justify="right")
    table.add_column("Flags", overflow="fold", max_width=60)

    for symbol, r in reports:
        score_color = "red" if r.rug_score >= 50 else ("yellow" if r.rug_score >= 25 else "green")
        mint_icon = "[red]X[/red]" if r.mint_authority_active else "[green]OK[/green]"
        freeze_icon = "[red]X[/red]" if r.freeze_authority_active else "[green]OK[/green]"
        flags_str = "; ".join(r.flags) if r.flags else "[green]clean[/green]"
        table.add_row(
            symbol[:10],
            f"[{score_color}]{r.rug_score:.0f}[/{score_color}]",
            mint_icon,
            freeze_icon,
            f"{r.transfer_fee_bps/100:.1f}%",
            f"{r.top_10_holder_pct:.0f}%",
            flags_str,
        )
    console.print(table)

    # Summary stats
    safe = sum(1 for _, r in reports if r.rug_score < 25)
    risky = sum(1 for _, r in reports if 25 <= r.rug_score < 50)
    danger = sum(1 for _, r in reports if r.rug_score >= 50)
    console.print(f"\n[green]Safe: {safe}[/green]  [yellow]Risky: {risky}[/yellow]  [red]Danger: {danger}[/red]")
    console.print("\nResults saved to rug_reports table in data/memecoins.db")


@click.command()
@click.option("--recheck-all", is_flag=True, help="Re-check tokens even if previously analyzed.")
@click.option("--limit", type=int, default=0, help="Max tokens to check (0 = no limit).")
def main(recheck_all, limit):
    asyncio.run(main_async(recheck_all=recheck_all, limit=limit))


if __name__ == "__main__":
    main()
