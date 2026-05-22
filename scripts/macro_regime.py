"""Macro regime + Fear and Greed scorer.

Pulls free public APIs:
  - Alternative.me Fear and Greed Index
  - CoinGecko (BTC dominance, total mcap, USDC/USDT supply)
  - FRED (Fed balance sheet WALCL, M2 money supply)

Computes macro_regime_score (0-100) where:
  - Below 30 = macro headwind (Fed tightening, BTC dom rising, greed extreme, stables shrinking)
  - 30-70 = neutral / mixed signals
  - Above 70 = macro tailwind (Fed easing, BTC dom falling, fear contrarian buy zone, stables growing)

Strategy reads from macro_regime table to adjust position sizing.
"""
from __future__ import annotations

import asyncio
import json
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

load_dotenv(dotenv_path=Path('.env'))

DB_PATH = Path("data/memecoins.db")

# FRED API key — optional. Free at https://fred.stlouisfed.org/docs/api/api_key.html
# Without it we skip Fed data and use the other 4 components with adjusted weights.
FRED_API_KEY = os.getenv("FRED_API_KEY")


@dataclass
class MacroSnapshot:
    timestamp: int
    fear_greed_value: int  # 0-100
    fear_greed_7d_avg: float
    btc_dominance: float
    btc_dominance_7d_ago: float
    total_crypto_mcap_usd: float
    total_crypto_mcap_7d_ago: float
    usdc_supply: float
    usdc_supply_30d_ago: float
    usdt_supply: float
    usdt_supply_30d_ago: float
    fed_balance_sheet_usd: float
    fed_balance_sheet_4w_ago: float
    macro_regime_score: float
    components: dict


def init_macro_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS macro_regime (
            timestamp INTEGER PRIMARY KEY,
            fear_greed_value INTEGER,
            fear_greed_7d_avg REAL,
            btc_dominance REAL,
            btc_dominance_delta_7d REAL,
            total_mcap_usd REAL,
            total_mcap_delta_7d_pct REAL,
            usdc_supply REAL,
            usdt_supply REAL,
            stable_supply_delta_30d_pct REAL,
            fed_balance_sheet_usd REAL,
            fed_balance_sheet_delta_4w_pct REAL,
            macro_regime_score REAL,
            components_json TEXT
        )
    """)
    conn.commit()
    conn.close()


# -------- Data fetchers --------

async def fetch_fear_greed(client: httpx.AsyncClient) -> tuple[int, float] | None:
    """Returns (current value, 7-day average)."""
    try:
        r = await client.get("https://api.alternative.me/fng/?limit=7", timeout=20.0)
        if r.status_code != 200:
            return None
        data = r.json()
        items = data.get("data", [])
        if not items:
            return None
        current = int(items[0].get("value", 50))
        vals = [int(x.get("value", 50)) for x in items]
        avg_7d = sum(vals) / len(vals)
        return current, avg_7d
    except Exception:
        return None


async def fetch_btc_dominance(client: httpx.AsyncClient) -> tuple[float, float] | None:
    """Returns (current BTC dominance %, BTC dominance 7 days ago %)."""
    try:
        # CoinGecko global endpoint for current
        r = await client.get("https://api.coingecko.com/api/v3/global", timeout=20.0)
        if r.status_code != 200:
            return None
        data = r.json().get("data", {})
        current_dom = float(data.get("market_cap_percentage", {}).get("btc", 0))

        # For 7d-ago, use the historical endpoint via the bitcoin coin's market chart
        # We compute dominance approximation: BTC market cap / total market cap
        r2 = await client.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
            params={"vs_currency": "usd", "days": 7, "interval": "daily"},
            timeout=20.0,
        )
        if r2.status_code != 200:
            return current_dom, current_dom  # fallback: assume no change
        chart = r2.json()
        btc_mcaps = chart.get("market_caps", [])
        if len(btc_mcaps) < 2:
            return current_dom, current_dom

        # 7d ago BTC mcap
        btc_7d_ago = btc_mcaps[0][1]
        # Get total mcap 7d ago from /global/decentralized_finance_defi? No — use total
        r3 = await client.get(
            "https://api.coingecko.com/api/v3/global", timeout=20.0
        )
        # We don't have a clean historical total mcap endpoint on free tier, so approximate
        # by assuming total grew proportionally. This is rough but functional.
        # Better: just compute delta from current vs the snapshot stored previously.
        prev_dom = get_previous_btc_dominance(days_ago=7)
        if prev_dom is None:
            prev_dom = current_dom  # first run — no historical comparison
        return current_dom, prev_dom
    except Exception:
        return None


async def fetch_total_mcap(client: httpx.AsyncClient) -> tuple[float, float] | None:
    """Returns (current total crypto market cap, 7d ago market cap)."""
    try:
        r = await client.get("https://api.coingecko.com/api/v3/global", timeout=20.0)
        if r.status_code != 200:
            return None
        data = r.json().get("data", {})
        current = float(data.get("total_market_cap", {}).get("usd", 0))
        prev = get_previous_total_mcap(days_ago=7)
        if prev is None:
            prev = current
        return current, prev
    except Exception:
        return None


async def fetch_stable_supplies(client: httpx.AsyncClient) -> tuple[float, float, float, float] | None:
    """Returns (usdc_supply, usdc_30d_ago, usdt_supply, usdt_30d_ago)."""
    try:
        # USDC market cap via CoinGecko
        usdc_r = await client.get(
            "https://api.coingecko.com/api/v3/coins/usd-coin",
            params={"localization": "false", "tickers": "false",
                    "market_data": "true", "community_data": "false",
                    "developer_data": "false"},
            timeout=20.0,
        )
        usdc_mcap = 0.0
        if usdc_r.status_code == 200:
            usdc_mcap = float(usdc_r.json().get("market_data", {}).get("market_cap", {}).get("usd", 0))

        await asyncio.sleep(1.0)  # CoinGecko rate limit

        usdt_r = await client.get(
            "https://api.coingecko.com/api/v3/coins/tether",
            params={"localization": "false", "tickers": "false",
                    "market_data": "true", "community_data": "false",
                    "developer_data": "false"},
            timeout=20.0,
        )
        usdt_mcap = 0.0
        if usdt_r.status_code == 200:
            usdt_mcap = float(usdt_r.json().get("market_data", {}).get("market_cap", {}).get("usd", 0))

        usdc_30d = get_previous_stable_supply("usdc", days_ago=30) or usdc_mcap
        usdt_30d = get_previous_stable_supply("usdt", days_ago=30) or usdt_mcap
        return usdc_mcap, usdc_30d, usdt_mcap, usdt_30d
    except Exception:
        return None


async def fetch_fed_balance_sheet(client: httpx.AsyncClient) -> tuple[float, float] | None:
    """Returns (latest WALCL value, 4-weeks-ago WALCL value). Both in millions USD."""
    if not FRED_API_KEY:
        return None
    try:
        # Get last 8 weekly observations
        r = await client.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "WALCL",
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 8,
            },
            timeout=20.0,
        )
        if r.status_code != 200:
            return None
        obs = r.json().get("observations", [])
        if len(obs) < 5:
            return None
        # WALCL is in millions of USD
        latest = float(obs[0]["value"]) * 1_000_000
        four_weeks_ago = float(obs[4]["value"]) * 1_000_000
        return latest, four_weeks_ago
    except Exception:
        return None


# -------- Historical lookups from our own DB --------

def get_previous_btc_dominance(days_ago: int) -> float | None:
    conn = sqlite3.connect(DB_PATH)
    cutoff = int(time.time()) - (days_ago * 86400)
    row = conn.execute("""
        SELECT btc_dominance FROM macro_regime
        WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1
    """, (cutoff,)).fetchone()
    conn.close()
    return row[0] if row else None


def get_previous_total_mcap(days_ago: int) -> float | None:
    conn = sqlite3.connect(DB_PATH)
    cutoff = int(time.time()) - (days_ago * 86400)
    row = conn.execute("""
        SELECT total_mcap_usd FROM macro_regime
        WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1
    """, (cutoff,)).fetchone()
    conn.close()
    return row[0] if row else None


def get_previous_stable_supply(stable: str, days_ago: int) -> float | None:
    conn = sqlite3.connect(DB_PATH)
    cutoff = int(time.time()) - (days_ago * 86400)
    col = f"{stable}_supply"
    row = conn.execute(f"""
        SELECT {col} FROM macro_regime
        WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1
    """, (cutoff,)).fetchone()
    conn.close()
    return row[0] if row else None


# -------- Scoring --------

def score_fear_greed(value: int) -> float:
    """Contrarian scoring: extreme fear is bullish, extreme greed is bearish.
    Returns 0-100 contribution (higher = more bullish for forward returns).
    """
    if value <= 20:
        return 90
    elif value <= 35:
        return 70
    elif value <= 55:
        return 50
    elif value <= 70:
        return 40
    elif value <= 85:
        return 25
    else:
        return 10


def score_btc_dominance_trend(current: float, previous: float) -> float:
    """Falling BTC dominance = bullish for alts/memecoins."""
    if previous == 0 or current == previous:
        return 50
    delta = current - previous
    # Each percentage point change = 10 score points (capped)
    score = 50 - (delta * 10)
    return max(0, min(100, score))


def score_total_mcap_trend(current: float, previous: float) -> float:
    """Growing total mcap = risk-on environment."""
    if previous == 0:
        return 50
    delta_pct = ((current - previous) / previous) * 100
    score = 50 + (delta_pct * 3)
    return max(0, min(100, score))


def score_stable_supply_trend(usdc_now: float, usdc_prev: float,
                                usdt_now: float, usdt_prev: float) -> float:
    """Growing stablecoin supply = new money flowing into crypto."""
    if usdc_prev == 0 and usdt_prev == 0:
        return 50
    total_now = usdc_now + usdt_now
    total_prev = (usdc_prev or usdc_now) + (usdt_prev or usdt_now)
    if total_prev == 0:
        return 50
    delta_pct = ((total_now - total_prev) / total_prev) * 100
    # 5% monthly growth = strong bullish (score 80)
    score = 50 + (delta_pct * 6)
    return max(0, min(100, score))


def score_fed_balance_sheet(current: float, previous: float) -> float:
    """Expanding Fed balance sheet = liquidity injection = bullish for risk assets."""
    if previous == 0:
        return 50
    delta_pct = ((current - previous) / previous) * 100
    # 0.5% over 4 weeks = strong expansion = bullish
    score = 50 + (delta_pct * 30)
    return max(0, min(100, score))


def compute_macro_score(snapshot: MacroSnapshot) -> tuple[float, dict]:
    """Weighted combination of all components. Adjust weights if FRED is missing."""
    fg_score = score_fear_greed(snapshot.fear_greed_value)
    btc_dom_score = score_btc_dominance_trend(snapshot.btc_dominance, snapshot.btc_dominance_7d_ago)
    mcap_score = score_total_mcap_trend(snapshot.total_crypto_mcap_usd, snapshot.total_crypto_mcap_7d_ago)
    stable_score = score_stable_supply_trend(
        snapshot.usdc_supply, snapshot.usdc_supply_30d_ago,
        snapshot.usdt_supply, snapshot.usdt_supply_30d_ago,
    )
    fed_available = snapshot.fed_balance_sheet_usd > 0
    fed_score = (score_fed_balance_sheet(snapshot.fed_balance_sheet_usd,
                                          snapshot.fed_balance_sheet_4w_ago)
                 if fed_available else None)

    if fed_available:
        weights = {"fear_greed": 0.25, "btc_dominance": 0.20, "stable_supply": 0.20,
                   "total_mcap": 0.15, "fed_balance": 0.20}
        weighted = (fg_score * 0.25 + btc_dom_score * 0.20 + stable_score * 0.20 +
                    mcap_score * 0.15 + fed_score * 0.20)
    else:
        # Without Fed data, redistribute its 20% weight: 8% to F&G, 6% to BTC dom, 6% to stables
        weights = {"fear_greed": 0.33, "btc_dominance": 0.26, "stable_supply": 0.26,
                   "total_mcap": 0.15}
        weighted = (fg_score * 0.33 + btc_dom_score * 0.26 + stable_score * 0.26 +
                    mcap_score * 0.15)

    components = {
        "fear_greed": {"value": snapshot.fear_greed_value, "score": round(fg_score, 1),
                       "weight": weights["fear_greed"]},
        "btc_dominance": {"current": round(snapshot.btc_dominance, 2),
                          "prev_7d": round(snapshot.btc_dominance_7d_ago, 2),
                          "score": round(btc_dom_score, 1),
                          "weight": weights["btc_dominance"]},
        "total_mcap": {"current_usd": snapshot.total_crypto_mcap_usd,
                       "prev_7d_usd": snapshot.total_crypto_mcap_7d_ago,
                       "score": round(mcap_score, 1),
                       "weight": weights["total_mcap"]},
        "stable_supply": {"usdc_now": snapshot.usdc_supply,
                          "usdt_now": snapshot.usdt_supply,
                          "score": round(stable_score, 1),
                          "weight": weights["stable_supply"]},
        "fed_balance": {"current_usd": snapshot.fed_balance_sheet_usd,
                        "prev_4w_usd": snapshot.fed_balance_sheet_4w_ago,
                        "score": round(fed_score, 1) if fed_score is not None else None,
                        "weight": weights.get("fed_balance", 0.0),
                        "available": fed_available},
    }
    return round(weighted, 1), components


# -------- Save / display --------

def save_macro_snapshot(snap: MacroSnapshot) -> None:
    conn = sqlite3.connect(DB_PATH)
    btc_dom_delta = snap.btc_dominance - snap.btc_dominance_7d_ago
    mcap_delta_pct = (((snap.total_crypto_mcap_usd - snap.total_crypto_mcap_7d_ago)
                       / snap.total_crypto_mcap_7d_ago) * 100
                      if snap.total_crypto_mcap_7d_ago else 0)
    stable_now = snap.usdc_supply + snap.usdt_supply
    stable_prev = (snap.usdc_supply_30d_ago or snap.usdc_supply) + (snap.usdt_supply_30d_ago or snap.usdt_supply)
    stable_delta_pct = ((stable_now - stable_prev) / stable_prev * 100) if stable_prev else 0
    fed_delta_pct = (((snap.fed_balance_sheet_usd - snap.fed_balance_sheet_4w_ago)
                      / snap.fed_balance_sheet_4w_ago) * 100
                     if snap.fed_balance_sheet_4w_ago else 0)
    conn.execute("""
        INSERT OR REPLACE INTO macro_regime
        (timestamp, fear_greed_value, fear_greed_7d_avg, btc_dominance,
         btc_dominance_delta_7d, total_mcap_usd, total_mcap_delta_7d_pct,
         usdc_supply, usdt_supply, stable_supply_delta_30d_pct,
         fed_balance_sheet_usd, fed_balance_sheet_delta_4w_pct,
         macro_regime_score, components_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        snap.timestamp, snap.fear_greed_value, snap.fear_greed_7d_avg,
        snap.btc_dominance, btc_dom_delta,
        snap.total_crypto_mcap_usd, mcap_delta_pct,
        snap.usdc_supply, snap.usdt_supply, stable_delta_pct,
        snap.fed_balance_sheet_usd, fed_delta_pct,
        snap.macro_regime_score, json.dumps(snap.components),
    ))
    conn.commit()
    conn.close()


def display_results(console: Console, snap: MacroSnapshot) -> None:
    score = snap.macro_regime_score
    if score >= 70:
        regime = "[green]TAILWIND[/green]"
    elif score <= 30:
        regime = "[red]HEADWIND[/red]"
    else:
        regime = "[yellow]NEUTRAL[/yellow]"

    console.print(f"\n[bold]Macro Regime Score: {score:.1f}/100 — {regime}[/bold]\n")

    table = Table(title="Component Breakdown")
    table.add_column("Component")
    table.add_column("Reading")
    table.add_column("Score", justify="right")
    table.add_column("Weight", justify="right")
    table.add_column("Interpretation")

    c = snap.components

    # Fear & Greed
    fg = c["fear_greed"]
    fg_value = fg["value"]
    fg_label = ("Extreme Fear" if fg_value <= 25 else
                "Fear" if fg_value <= 45 else
                "Neutral" if fg_value <= 55 else
                "Greed" if fg_value <= 75 else "Extreme Greed")
    fg_interp = "contrarian buy zone" if fg_value <= 25 else (
                "selling pressure ahead" if fg_value >= 75 else "mixed")
    table.add_row("Fear & Greed", f"{fg_value} ({fg_label})",
                  f"{fg['score']:.0f}", f"{fg['weight']*100:.0f}%", fg_interp)

    # BTC Dominance
    btc = c["btc_dominance"]
    btc_change = btc["current"] - btc["prev_7d"]
    btc_interp = ("alt season trend" if btc_change < -0.5 else
                  "BTC season trend" if btc_change > 0.5 else "stable")
    table.add_row("BTC Dominance", f"{btc['current']:.1f}% (7d: {btc['prev_7d']:.1f}%)",
                  f"{btc['score']:.0f}", f"{btc['weight']*100:.0f}%", btc_interp)

    # Total Mcap
    mc = c["total_mcap"]
    mc_delta = ((mc["current_usd"] - mc["prev_7d_usd"]) / mc["prev_7d_usd"] * 100
                if mc["prev_7d_usd"] else 0)
    mc_interp = ("risk-on expanding" if mc_delta > 2 else
                 "risk-off contracting" if mc_delta < -2 else "flat")
    table.add_row("Total Crypto Mcap",
                  f"${mc['current_usd']/1e12:.2f}T (7d: {mc_delta:+.1f}%)",
                  f"{mc['score']:.0f}", f"{mc['weight']*100:.0f}%", mc_interp)

    # Stables
    st = c["stable_supply"]
    total_stable_b = (st["usdc_now"] + st["usdt_now"]) / 1e9
    stable_interp = ("new money flowing in" if st["score"] > 60 else
                     "money exiting" if st["score"] < 40 else "steady")
    table.add_row("Stablecoin Supply", f"${total_stable_b:.0f}B (USDC+USDT)",
                  f"{st['score']:.0f}", f"{st['weight']*100:.0f}%", stable_interp)

    # Fed
    fed = c["fed_balance"]
    if fed["available"]:
        fed_delta_pct = ((fed["current_usd"] - fed["prev_4w_usd"]) / fed["prev_4w_usd"] * 100
                         if fed["prev_4w_usd"] else 0)
        fed_interp = ("liquidity expansion" if fed_delta_pct > 0.1 else
                      "liquidity contraction" if fed_delta_pct < -0.1 else "flat")
        table.add_row("Fed Balance Sheet",
                      f"${fed['current_usd']/1e12:.2f}T (4w: {fed_delta_pct:+.2f}%)",
                      f"{fed['score']:.0f}", f"{fed['weight']*100:.0f}%", fed_interp)
    else:
        table.add_row("Fed Balance Sheet", "[dim]FRED_API_KEY not set, skipping[/dim]",
                      "-", "-", "-")

    console.print(table)

    # Strategy implications
    console.print("\n[bold]Strategy implications:[/bold]")
    if score >= 70:
        console.print("  [green]Tailwind regime — strategy can use full position sizes[/green]")
    elif score >= 50:
        console.print("  [yellow]Mildly favorable — normal position sizes[/yellow]")
    elif score >= 30:
        console.print("  [yellow]Neutral / mixed — reduce position sizes 25-50%[/yellow]")
    else:
        console.print("  [red]Headwind regime — reduce sizes 50-75% or pause new entries[/red]")


async def main_async():
    init_macro_db()
    console = Console()
    console.print("[bold]Macro Regime + Fear & Greed[/bold]\n")
    console.print("Fetching from Alternative.me, CoinGecko, and FRED...\n")

    async with httpx.AsyncClient() as client:
        # Sequential to be nice to free APIs
        fg = await fetch_fear_greed(client)
        await asyncio.sleep(0.5)
        btc = await fetch_btc_dominance(client)
        await asyncio.sleep(1.0)
        mcap = await fetch_total_mcap(client)
        await asyncio.sleep(1.0)
        stable = await fetch_stable_supplies(client)
        await asyncio.sleep(0.5)
        fed = await fetch_fed_balance_sheet(client) if FRED_API_KEY else None

    if fg is None:
        console.print("[red]Failed to fetch Fear & Greed Index — aborting[/red]")
        return
    if btc is None or mcap is None:
        console.print("[red]Failed to fetch CoinGecko data — aborting[/red]")
        return
    if stable is None:
        console.print("[yellow]Stablecoin data fetch failed, using zero values[/yellow]")
        stable = (0.0, 0.0, 0.0, 0.0)
    if fed is None:
        fed = (0.0, 0.0)

    snap = MacroSnapshot(
        timestamp=int(time.time()),
        fear_greed_value=fg[0],
        fear_greed_7d_avg=fg[1],
        btc_dominance=btc[0],
        btc_dominance_7d_ago=btc[1],
        total_crypto_mcap_usd=mcap[0],
        total_crypto_mcap_7d_ago=mcap[1],
        usdc_supply=stable[0],
        usdc_supply_30d_ago=stable[1],
        usdt_supply=stable[2],
        usdt_supply_30d_ago=stable[3],
        fed_balance_sheet_usd=fed[0],
        fed_balance_sheet_4w_ago=fed[1],
        macro_regime_score=0.0,
        components={},
    )
    score, components = compute_macro_score(snap)
    snap.macro_regime_score = score
    snap.components = components

    save_macro_snapshot(snap)
    display_results(console, snap)

    console.print("\nSaved snapshot to macro_regime table.")
    if not FRED_API_KEY:
        console.print("\n[dim]Optional: add FRED_API_KEY to .env for Fed balance sheet data[/dim]")
        console.print("[dim]  Free at https://fred.stlouisfed.org/docs/api/api_key.html[/dim]")


@click.command()
def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
