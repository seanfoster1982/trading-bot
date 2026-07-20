"""Multi-timeframe OHLCV ingester for memecoin trading bot.

Pulls 5m, 15m, 1h, 4h, 1d candles from Birdeye for tokens that:
1. Pass current screener filters
2. Have a clean rug report (score < 25, freeze authority inactive)

Stores everything in candles table indexed by (symbol, interval, timestamp).
On subsequent runs, only fetches new candles since last fetch (incremental).
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import click
import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale_config import WHALE_ONLY, ACTIVE_SCREEN  # noqa: E402

load_dotenv()

BIRDEYE_BASE = "https://public-api.birdeye.so"
BIRDEYE_KEY = os.getenv("BIRDEYE_API_KEY")
DB_PATH = Path("data/memecoins.db")

# Timeframe configuration: (interval_label, seconds_per_candle, days_of_history)
TIMEFRAMES = [
    ("5m",  300,    14),    # 14 days of 5m candles
    ("15m", 900,    30),    # 30 days of 15m candles
    ("1H",  3600,   90),    # 90 days of 1h candles
    ("4H",  14400,  180),   # 180 days of 4h candles
    ("1D",  86400,  365),   # 365 days of daily candles
]

MAX_RUG_SCORE = 25  # Skip tokens above this rug score


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def init_candles_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            symbol TEXT NOT NULL,
            address TEXT NOT NULL,
            interval TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            PRIMARY KEY (address, interval, timestamp)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_candles_symbol_interval
        ON candles(symbol, interval, timestamp)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingest_log (
            address TEXT NOT NULL,
            interval TEXT NOT NULL,
            last_fetched_at INTEGER NOT NULL,
            last_candle_ts INTEGER NOT NULL,
            candles_total INTEGER NOT NULL,
            PRIMARY KEY (address, interval)
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


def get_tradeable_tokens() -> list[tuple[str, str, str]]:
    """Return [(symbol, address, screen), ...] for tokens that:
    - Were screened in last 24h
    - Have a rug report with score < MAX_RUG_SCORE
    - Don't have freeze authority active
    """
    conn = sqlite3.connect(DB_PATH)
    cutoff = int(time.time()) - (24 * 3600)
    if WHALE_ONLY:
        rows = conn.execute("""
            SELECT DISTINCT s.symbol, s.address, s.screen
            FROM screened_tokens s
            INNER JOIN rug_reports r ON s.address = r.address
            WHERE s.screened_at >= ?
              AND s.screen = ?
              AND r.rug_score < ?
              AND r.freeze_authority_active = 0
        """, (cutoff, ACTIVE_SCREEN, MAX_RUG_SCORE)).fetchall()
    else:
        rows = conn.execute("""
            SELECT DISTINCT s.symbol, s.address, s.screen
            FROM screened_tokens s
            INNER JOIN rug_reports r ON s.address = r.address
            WHERE s.screened_at >= ?
              AND r.rug_score < ?
              AND r.freeze_authority_active = 0
        """, (cutoff, MAX_RUG_SCORE)).fetchall()

    # Always keep ingesting tokens with open paper positions, even if they
    # dropped off the screen — exit checks need fresh prices.
    seen = {r[1] for r in rows}
    open_pos = conn.execute("""
        SELECT DISTINCT symbol, address FROM paper_trades
        WHERE closed_at IS NULL
    """).fetchall()
    rows = list(rows)
    for sym, addr in open_pos:
        if addr not in seen:
            rows.append((sym, addr, "open_position"))
    conn.close()
    return rows


def get_last_candle_ts(address: str, interval: str) -> int | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT last_candle_ts FROM ingest_log
        WHERE address = ? AND interval = ?
    """, (address, interval)).fetchone()
    conn.close()
    return row[0] if row else None


async def fetch_ohlcv(
    client: httpx.AsyncClient,
    address: str,
    interval: str,
    time_from: int,
    time_to: int,
) -> tuple[list[Candle], str | None]:
    """Single OHLCV API call to Birdeye. Returns ([], error_str) on failure."""
    headers = {
        "X-API-KEY": BIRDEYE_KEY,
        "x-chain": "solana",
        "accept": "application/json",
    }
    params = {
        "address": address,
        "type": interval,
        "time_from": time_from,
        "time_to": time_to,
    }
    try:
        r = await client.get(
            f"{BIRDEYE_BASE}/defi/ohlcv",
            headers=headers,
            params=params,
            timeout=30.0,
        )
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        if not data.get("success"):
            return [], f"API error: {data.get('message', 'unknown')}"
        items = data.get("data", {}).get("items", []) or []
        candles = []
        for it in items:
            candles.append(Candle(
                timestamp=to_int(it.get("unixTime")),
                open=to_float(it.get("o")),
                high=to_float(it.get("h")),
                low=to_float(it.get("l")),
                close=to_float(it.get("c")),
                volume=to_float(it.get("v")),
            ))
        return candles, None
    except Exception as e:
        return [], f"Exception: {str(e)[:200]}"


def save_candles(symbol: str, address: str, interval: str, candles: list[Candle]) -> int:
    if not candles:
        return 0
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    inserted = 0
    for c in candles:
        try:
            cur.execute("""
                INSERT OR IGNORE INTO candles
                (symbol, address, interval, timestamp, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (symbol, address, interval, c.timestamp,
                  c.open, c.high, c.low, c.close, c.volume))
            inserted += cur.rowcount
        except Exception:
            continue
    # Update ingest log
    last_ts = max(c.timestamp for c in candles)
    total = conn.execute("""
        SELECT COUNT(*) FROM candles WHERE address = ? AND interval = ?
    """, (address, interval)).fetchone()[0]
    cur.execute("""
        INSERT OR REPLACE INTO ingest_log
        (address, interval, last_fetched_at, last_candle_ts, candles_total)
        VALUES (?, ?, ?, ?, ?)
    """, (address, interval, int(time.time()), last_ts, total))
    conn.commit()
    conn.close()
    return inserted


async def ingest_token(
    client: httpx.AsyncClient,
    symbol: str,
    address: str,
    console: Console,
) -> dict:
    """Pull all timeframes for a single token. Returns summary dict."""
    now_ts = int(time.time())
    results = {}

    for interval, seconds_per_candle, history_days in TIMEFRAMES:
        # Determine fetch window
        last_ts = get_last_candle_ts(address, interval)
        full_history_start = now_ts - (history_days * 86400)
        if last_ts is None:
            time_from = full_history_start
            mode = "backfill"
        else:
            time_from = last_ts + 1
            mode = "incremental"
            # Skip if we already have very recent data (less than one candle's worth)
            if (now_ts - last_ts) < seconds_per_candle:
                results[interval] = {"mode": "skip", "new": 0, "err": None}
                continue

        # Birdeye returns up to ~1000 candles per call. For long backfills we paginate.
        max_candles_per_call = 1000
        max_window = max_candles_per_call * seconds_per_candle

        total_new = 0
        last_err = None
        window_start = time_from
        while window_start < now_ts:
            window_end = min(window_start + max_window, now_ts)
            candles, err = await fetch_ohlcv(
                client, address, interval, window_start, window_end
            )
            if err:
                last_err = err
                break
            if not candles:
                break
            inserted = save_candles(symbol, address, interval, candles)
            total_new += inserted
            # Advance window — start from the last candle's timestamp + 1
            window_start = max(c.timestamp for c in candles) + seconds_per_candle
            # Throttle
            await asyncio.sleep(0.15)

        results[interval] = {"mode": mode, "new": total_new, "err": last_err}

    return results


async def main_async(*, only_symbol: str | None, force_backfill: bool):
    init_candles_db()
    if not BIRDEYE_KEY:
        Console().print("[red]BIRDEYE_API_KEY not set in .env[/red]")
        return

    console = Console()
    console.print("[bold]Multi-timeframe OHLCV Ingester[/bold]\n")

    # Get tradeable token universe
    tokens = get_tradeable_tokens()
    if only_symbol:
        tokens = [(s, a, sc) for s, a, sc in tokens if s.upper() == only_symbol.upper()]
    if not tokens:
        console.print("[yellow]No tradeable tokens found.[/yellow]")
        console.print("  Run: python scripts/screen_memecoins.py")
        console.print("  Then: python scripts/rug_check.py")
        console.print("  Then this script again.")
        return

    # Deduplicate by address (a token might pass multiple screens)
    seen = set()
    unique_tokens = []
    for sym, addr, screen in tokens:
        if addr not in seen:
            seen.add(addr)
            unique_tokens.append((sym, addr, screen))

    console.print(f"Tradeable universe: {len(unique_tokens)} tokens\n")

    # If force backfill, wipe ingest log so everything refetches
    if force_backfill:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM ingest_log")
        conn.execute("DELETE FROM candles")
        conn.commit()
        conn.close()
        console.print("[yellow]Force backfill: cleared candles and ingest_log[/yellow]\n")

    results_summary = []
    async with httpx.AsyncClient() as client:
        for i, (sym, addr, screen) in enumerate(unique_tokens, 1):
            console.print(f"[cyan][{i}/{len(unique_tokens)}] {sym}[/cyan] ({addr[:8]}...{addr[-6:]})")
            results = await ingest_token(client, sym, addr, console)
            total_new = sum(r["new"] for r in results.values())
            errors = sum(1 for r in results.values() if r["err"])
            for interval, r in results.items():
                mode_indicator = {"backfill": "BF", "incremental": "INC", "skip": "--"}[r["mode"]]
                err_str = f" [red]ERR: {r['err'][:60]}[/red]" if r["err"] else ""
                console.print(f"    {interval:<4} [{mode_indicator}]: {r['new']:>5} new candles{err_str}")
            results_summary.append({
                "symbol": sym, "address": addr,
                "total_new": total_new, "errors": errors,
            })
            console.print()

    # Final summary table
    table = Table(title="OHLCV Ingest Summary")
    table.add_column("Symbol")
    table.add_column("Total new candles", justify="right")
    table.add_column("5m", justify="right")
    table.add_column("15m", justify="right")
    table.add_column("1H", justify="right")
    table.add_column("4H", justify="right")
    table.add_column("1D", justify="right")
    table.add_column("Errors", justify="right")

    conn = sqlite3.connect(DB_PATH)
    for r in results_summary:
        counts = {}
        for interval, _, _ in TIMEFRAMES:
            row = conn.execute("""
                SELECT COUNT(*) FROM candles WHERE address = ? AND interval = ?
            """, (r["address"], interval)).fetchone()
            counts[interval] = row[0] if row else 0
        err_color = "red" if r["errors"] > 0 else "green"
        table.add_row(
            r["symbol"][:10],
            str(r["total_new"]),
            str(counts.get("5m", 0)),
            str(counts.get("15m", 0)),
            str(counts.get("1H", 0)),
            str(counts.get("4H", 0)),
            str(counts.get("1D", 0)),
            f"[{err_color}]{r['errors']}[/{err_color}]",
        )
    conn.close()
    console.print(table)

    total_candles_db = sum(
        sum(results_summary[i]["total_new"] for i in range(len(results_summary)))
        for _ in [0]
    )
    console.print(f"\nNext: indicator computer + whale risk scoring")


@click.command()
@click.option("--only-symbol", default=None, help="Only ingest one symbol (for testing).")
@click.option("--force-backfill", is_flag=True, help="Wipe and refetch all candles.")
def main(only_symbol, force_backfill):
    asyncio.run(main_async(only_symbol=only_symbol, force_backfill=force_backfill))


if __name__ == "__main__":
    main()
