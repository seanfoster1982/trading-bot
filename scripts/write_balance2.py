from pathlib import Path

content = """\"\"\"Balance reader - Piece 2 of the execution adapter. Rewritten to use raw JSON-RPC.

Reads SOL + SPL token balances for the bot wallet via Helius RPC.
Uses raw httpx instead of solana-py wrappers to avoid parsing inconsistencies.
\"\"\"
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.wallet_loader import verify_wallet
from scripts.token_metadata import get_cached as get_cached_metadata
from scripts.token_metadata import init_metadata_db

load_dotenv(dotenv_path=Path('.env'))

HELIUS_KEY = os.getenv('HELIUS_API_KEY')
if not HELIUS_KEY:
    raise SystemExit('ERROR: HELIUS_API_KEY not set in .env')
SOLANA_RPC = f'https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}'

USDC_MINT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
USDC_DECIMALS = 6
SOL_DECIMALS = 9
LAMPORTS_PER_SOL = 10 ** SOL_DECIMALS
SPL_TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'


@dataclass
class TokenBalance:
    symbol: str
    mint: str
    raw_amount: int
    decimals: int
    ui_amount: float


async def rpc_call(client, method, params):
    \"\"\"Make a raw JSON-RPC call to Helius. Raise on bad responses.\"\"\"
    r = await client.post(SOLANA_RPC, json={
        'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params,
    }, timeout=30.0)
    if r.status_code != 200:
        raise RuntimeError(f'RPC HTTP {r.status_code}: {r.text[:200]}')
    data = r.json()
    if 'error' in data:
        raise RuntimeError(f'RPC error: {data[\"error\"]}')
    if 'result' not in data:
        raise RuntimeError(f'RPC malformed response: {data}')
    return data['result']


async def get_sol_balance(client, owner_address):
    result = await rpc_call(client, 'getBalance', [owner_address])
    if 'value' not in result:
        raise RuntimeError(f'getBalance missing value: {result}')
    lamports = result['value']
    if lamports is None:
        raise RuntimeError('getBalance returned value=None')
    return lamports / LAMPORTS_PER_SOL


async def get_token_balances(client, owner_address):
    result = await rpc_call(client, 'getTokenAccountsByOwner', [
        owner_address,
        {'programId': SPL_TOKEN_PROGRAM},
        {'encoding': 'jsonParsed'},
    ])
    balances = []
    for acc in result.get('value', []):
        try:
            info = acc['account']['data']['parsed']['info']
            mint = info['mint']
            ta = info['tokenAmount']
            raw = int(ta['amount'])
            decimals = int(ta['decimals'])
            if raw == 0:
                continue
            ui = raw / (10 ** decimals)
            meta = get_cached_metadata(mint)
            symbol = meta['symbol'] if meta else ('USDC' if mint == USDC_MINT else '?')
            balances.append(TokenBalance(
                symbol=symbol, mint=mint, raw_amount=raw,
                decimals=decimals, ui_amount=ui,
            ))
        except (KeyError, TypeError, ValueError) as e:
            print(f'  [warn] skipped malformed token account: {type(e).__name__}: {e}')
            continue
    return balances


async def read_balances():
    init_metadata_db()
    console = Console()
    console.print('[bold]Balance Reader[/bold]\\n')

    kp = verify_wallet()
    owner_address = str(kp.pubkey())
    console.print()

    rpc_safe = SOLANA_RPC.split('api-key=')[0] + 'api-key=***'
    console.print(f'Querying Solana RPC: {rpc_safe}')
    console.print(f'Wallet: {owner_address}\\n')

    async with httpx.AsyncClient() as client:
        try:
            sol = await get_sol_balance(client, owner_address)
        except Exception as e:
            console.print(f'[red]Failed SOL balance: {e}[/red]')
            return
        try:
            tokens = await get_token_balances(client, owner_address)
        except Exception as e:
            console.print(f'[red]Failed token balances: {e}[/red]')
            tokens = []

    usdc = next((t for t in tokens if t.mint == USDC_MINT), None)
    others = [t for t in tokens if t.mint != USDC_MINT]

    table = Table(title='Wallet Balances')
    table.add_column('Asset')
    table.add_column('Amount', justify='right')
    table.add_column('Mint / Note')
    table.add_row('SOL', f'{sol:.6f}', 'native')
    if usdc:
        table.add_row('USDC', f'{usdc.ui_amount:.4f}', 'EPjFWd...TDt1v')
    else:
        table.add_row('USDC', '0.0000', '(no USDC token account)')
    # Cap display - the wallet has 448 accounts but most are zero
    for t in others[:15]:
        mint_short = f'{t.mint[:6]}...{t.mint[-6:]}'
        table.add_row(t.symbol, f'{t.ui_amount:,.4f}', mint_short)
    if len(others) > 15:
        table.add_row('...', f'+{len(others) - 15} more', '(see logs)')

    console.print(table)
    console.print()
    console.print(f'  SOL: {sol:.6f}')
    if usdc:
        console.print(f'  USDC: {usdc.ui_amount:.4f}')
    console.print(f'  Non-USDC token positions (with balance > 0): {len(others)}')


if __name__ == '__main__':
    asyncio.run(read_balances())
"""

Path('scripts/balance_reader.py').write_text(content, encoding='utf-8')
print('Wrote scripts/balance_reader.py')

import ast
try:
    ast.parse(content)
    print('Syntax OK')
except SyntaxError as e:
    print(f'Syntax error: {e}')
