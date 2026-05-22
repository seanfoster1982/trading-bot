from pathlib import Path

content = """\"\"\"Token metadata cache - decimals, supply, and other stable token attributes.

Decimals never change for a given token, so we cache forever.
First lookup hits Birdeye. Subsequent lookups read from local SQLite.

This module exposes:
  get_decimals(address) -> int        # main function, used everywhere
  backfill_all(addresses)             # warm the cache for many tokens at once

Used by: jupiter_quote.py, future execution.py, anywhere we convert between
human-readable amounts (1.5 tokens) and atomic amounts (1500000 atomic units for 6-decimal token).
\"\"\"
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv(dotenv_path=Path('.env'))

BIRDEYE_BASE = 'https://public-api.birdeye.so'
BIRDEYE_KEY = os.getenv('BIRDEYE_API_KEY')
DB_PATH = Path('data/memecoins.db')


def init_metadata_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(\"\"\"
        CREATE TABLE IF NOT EXISTS token_metadata (
            address TEXT PRIMARY KEY,
            symbol TEXT,
            decimals INTEGER NOT NULL,
            total_supply REAL,
            fetched_at INTEGER NOT NULL
        )
    \"\"\")
    conn.commit()
    conn.close()


def get_cached(address: str) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        'SELECT symbol, decimals, total_supply, fetched_at FROM token_metadata WHERE address = ?',
        (address,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {'symbol': row[0], 'decimals': row[1],
            'total_supply': row[2], 'fetched_at': row[3]}


def save_metadata(address: str, symbol: str, decimals: int,
                   total_supply: Optional[float]) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(\"\"\"
        INSERT OR REPLACE INTO token_metadata
        (address, symbol, decimals, total_supply, fetched_at)
        VALUES (?, ?, ?, ?, ?)
    \"\"\", (address, symbol, decimals, total_supply, int(time.time())))
    conn.commit()
    conn.close()


async def fetch_from_birdeye(client: httpx.AsyncClient,
                               address: str) -> Optional[dict]:
    if not BIRDEYE_KEY:
        return None
    headers = {'X-API-KEY': BIRDEYE_KEY, 'x-chain': 'solana',
               'accept': 'application/json'}
    try:
        r = await client.get(f'{BIRDEYE_BASE}/defi/token_overview',
                              headers=headers,
                              params={'address': address},
                              timeout=15.0)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get('success'):
            return None
        d = data.get('data') or {}
        decimals = d.get('decimals')
        if decimals is None:
            return None
        return {
            'symbol': d.get('symbol') or '',
            'decimals': int(decimals),
            'total_supply': d.get('totalSupply'),
        }
    except Exception:
        return None


async def get_decimals_async(client: httpx.AsyncClient,
                              address: str) -> Optional[int]:
    \"\"\"Get decimals for a token. Hits cache first, falls back to Birdeye.
    Returns None if Birdeye fails - caller must handle.\"\"\"
    cached = get_cached(address)
    if cached:
        return cached['decimals']
    meta = await fetch_from_birdeye(client, address)
    if meta is None:
        return None
    save_metadata(address, meta['symbol'], meta['decimals'], meta['total_supply'])
    return meta['decimals']


def get_decimals(address: str) -> Optional[int]:
    \"\"\"Synchronous version for code that doesn't have an httpx client.
    Cache hit returns immediately; cache miss spins up a one-shot async fetch.\"\"\"
    cached = get_cached(address)
    if cached:
        return cached['decimals']
    async def _fetch():
        async with httpx.AsyncClient() as client:
            return await get_decimals_async(client, address)
    return asyncio.run(_fetch())


async def backfill_all(addresses: list[tuple[str, str]]) -> dict:
    \"\"\"Fetch decimals for all addresses not yet cached.
    Input: list of (symbol, address) tuples.
    Returns: dict of stats {fetched, skipped, failed}.\"\"\"
    init_metadata_db()
    stats = {'fetched': 0, 'skipped': 0, 'failed': []}
    async with httpx.AsyncClient() as client:
        for sym, addr in addresses:
            if get_cached(addr) is not None:
                stats['skipped'] += 1
                continue
            meta = await fetch_from_birdeye(client, addr)
            if meta is None:
                stats['failed'].append((sym, addr))
                continue
            # Use the symbol from screening rather than Birdeye's, since
            # screening symbols are what the user sees in tables
            save_metadata(addr, sym, meta['decimals'], meta['total_supply'])
            stats['fetched'] += 1
            await asyncio.sleep(0.15)  # gentle throttle
    return stats


def main():
    \"\"\"CLI: backfill decimals for all tokens currently in screened_tokens.\"\"\"
    init_metadata_db()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        'SELECT DISTINCT symbol, address FROM screened_tokens'
    ).fetchall()
    conn.close()
    print(f'Found {len(rows)} unique tokens to backfill')
    stats = asyncio.run(backfill_all(rows))
    print(f\"Fetched: {stats['fetched']}\")
    print(f\"Skipped (cached): {stats['skipped']}\")
    print(f\"Failed: {len(stats['failed'])}\")
    for sym, addr in stats['failed']:
        print(f'  - {sym}: {addr}')

    # Dump the full cache so we can verify
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        'SELECT symbol, decimals, total_supply FROM token_metadata ORDER BY symbol'
    ).fetchall()
    conn.close()
    print()
    print('Cached metadata:')
    for sym, dec, supply in rows:
        supply_str = f'{supply:,.0f}' if supply else '-'
        print(f'  {sym:12s} decimals={dec}  supply={supply_str}')


if __name__ == '__main__':
    main()
"""

Path('scripts/token_metadata.py').write_text(content, encoding='utf-8')
print('Wrote scripts/token_metadata.py')

import ast
try:
    ast.parse(content)
    print('Syntax OK')
except SyntaxError as e:
    print(f'Syntax error: {e}')
