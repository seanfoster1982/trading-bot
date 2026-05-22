from pathlib import Path

content = """\"\"\"Watchlist module - always-evaluate tokens regardless of screen filters.

Reads contract addresses from data/watchlist.txt (one per line, optionally with
a comment after #). These tokens are always included in the strategy evaluation
universe, even if Birdeye's top-N lists don't surface them.

Use this for tokens you've personally identified that the screener might miss
because they're below liquidity thresholds or off the trending lists.
\"\"\"
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path('data/memecoins.db')
WATCHLIST_PATH = Path('data/watchlist.txt')
BIRDEYE_KEY = os.getenv('BIRDEYE_API_KEY')


def read_watchlist() -> list[tuple[str, str]]:
    \"\"\"Return list of (address, comment) pairs from watchlist.txt.\"\"\"
    if not WATCHLIST_PATH.exists():
        return []
    out = []
    for raw in WATCHLIST_PATH.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if '#' in line:
            addr, _, comment = line.partition('#')
            out.append((addr.strip(), comment.strip()))
        else:
            out.append((line, ''))
    return out


def fetch_token_overview(address: str) -> Optional[dict]:
    \"\"\"Fetch a token's current market data from Birdeye.\"\"\"
    if not BIRDEYE_KEY:
        return None
    try:
        r = httpx.get(
            'https://public-api.birdeye.so/defi/v3/token/market-data',
            params={'address': address},
            headers={'X-API-KEY': BIRDEYE_KEY, 'x-chain': 'solana'},
            timeout=15.0,
        )
        if r.status_code != 200:
            return None
        return r.json().get('data')
    except Exception:
        return None


def fetch_token_metadata(address: str) -> Optional[dict]:
    \"\"\"Fetch a token's name, symbol, and decimals from Birdeye.\"\"\"
    if not BIRDEYE_KEY:
        return None
    try:
        r = httpx.get(
            'https://public-api.birdeye.so/defi/v3/token/meta-data/single',
            params={'address': address},
            headers={'X-API-KEY': BIRDEYE_KEY, 'x-chain': 'solana'},
            timeout=15.0,
        )
        if r.status_code != 200:
            return None
        return r.json().get('data')
    except Exception:
        return None


def upsert_watchlist_into_screened(verbose: bool = False) -> int:
    \"\"\"Add each watchlist token to screened_tokens with screen='watchlist'.

    Returns the number of tokens added/refreshed.
    \"\"\"
    entries = read_watchlist()
    if not entries:
        if verbose:
            print('Watchlist is empty.')
        return 0

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    count = 0
    now = int(time.time())

    for addr, comment in entries:
        if not addr or len(addr) < 32:
            if verbose:
                print(f'  SKIP invalid address: {addr!r}')
            continue

        market = fetch_token_overview(addr)
        meta = fetch_token_metadata(addr)

        if not market or not meta:
            if verbose:
                print(f'  SKIP {addr[:12]}... (Birdeye returned nothing)')
            continue

        symbol = meta.get('symbol', addr[:8])
        mcap = market.get('market_cap') or market.get('marketcap') or 0
        liq = market.get('liquidity', 0)
        vol_24h = market.get('volume_24h_usd') or market.get('v24hUSD') or 0
        price = market.get('price', 0)
        price_change_24h = market.get('price_change_24h_percent') or market.get('priceChange24hPercent') or 0
        holder_count = market.get('holder', 0)

        cur.execute(\"\"\"
            INSERT INTO screened_tokens
              (screen, symbol, address, market_cap, liquidity, volume_24h,
               price, price_change_24h, holder_count, listed_at, screened_at,
               exhaustion_score, price_change_1h, price_change_4h)
            VALUES ('watchlist', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, NULL)
        \"\"\", (symbol, addr, mcap, liq, vol_24h, price, price_change_24h,
              holder_count, now))
        count += 1
        if verbose:
            print(f'  + {symbol} ({addr[:12]}...) mcap=\${mcap:,.0f} liq=\${liq:,.0f}')
            if comment:
                print(f'    comment: {comment}')

    conn.commit()
    conn.close()
    return count


def _test():
    n = upsert_watchlist_into_screened(verbose=True)
    print(f'\\nAdded/refreshed {n} watchlist token(s) in screened_tokens.')
    if n > 0:
        print('Run paper_trader.py or strategy.py to evaluate them.')


if __name__ == '__main__':
    _test()
"""

Path('scripts/watchlist.py').write_text(content, encoding='utf-8')

# Strip stray backslash-dollar from f-strings
src = Path('scripts/watchlist.py').read_text(encoding='utf-8')
src = src.replace(r'\$', '$')
Path('scripts/watchlist.py').write_text(src, encoding='utf-8')

# Create the watchlist file with the two tokens we know about
Path('data').mkdir(parents=True, exist_ok=True)
watchlist_content = """# Watchlist - one Solana mint address per line
# Optional comment after # on each line
# These tokens are evaluated by the strategy regardless of screen filters

6veQU7HDdXV5DC2Eqhnri5q71gkMzG73qKkSSudnpump   # DUST - manually added 2026-05-15
6qdzMx4c9rL2X3Ns3SwZ8uEo4zReDPjdXpAEmpo7pump   # BABYTROLL - manually added 2026-05-15
"""
Path('data/watchlist.txt').write_text(watchlist_content, encoding='utf-8')

print('Wrote scripts/watchlist.py')
print('Wrote data/watchlist.txt (DUST and BABYTROLL pre-added)')

import ast
try:
    ast.parse(Path('scripts/watchlist.py').read_text(encoding='utf-8'))
    print('Syntax OK')
except SyntaxError as e:
    print(f'Syntax error: {e}')
