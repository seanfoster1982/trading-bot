from pathlib import Path

content = """\"\"\"Balance reader - Piece 2 of the execution adapter.

Read-only. Queries the bot wallet's SOL balance and SPL token balances
via the Solana RPC.

Used by:
  - Sanity checks before execution
  - Daily spend cap enforcement
  - Position reconciliation after swaps

This script can be run anytime as a 'what's in my wallet' check.
\"\"\"
from __future__ import annotations

import asyncio
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.pubkey import Pubkey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.wallet_loader import verify_wallet
from scripts.token_metadata import get_cached as get_cached_metadata
from scripts.token_metadata import init_metadata_db

# Public mainnet RPC. Rate-limited but fine for read-only balance queries.
# For execution we may switch to a paid/private endpoint.
SOLANA_RPC = 'https://api.mainnet-beta.solana.com'

USDC_MINT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
USDC_DECIMALS = 6

# Native SOL doesn't use SPL tokens - it has a direct lamport balance.
# 1 SOL = 1_000_000_000 lamports (9 decimals).
SOL_DECIMALS = 9
LAMPORTS_PER_SOL = 10 ** SOL_DECIMALS

DB_PATH = Path('data/memecoins.db')


@dataclass
class TokenBalance:
    symbol: str
    mint: str
    raw_amount: int
    decimals: int
    ui_amount: float


async def get_sol_balance(client: AsyncClient, owner: Pubkey) -> float:
    \"\"\"Native SOL balance in SOL units.\"\"\"
    resp = await client.get_balance(owner, commitment=Confirmed)
    lamports = resp.value if resp and resp.value is not None else 0
    return lamports / LAMPORTS_PER_SOL


async def get_token_balances(client: AsyncClient, owner: Pubkey) -> list[TokenBalance]:
    \"\"\"All SPL token accounts owned by this wallet.\"\"\"
    # SPL Token program ID
    token_program = Pubkey.from_string('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA')
    resp = await client.get_token_accounts_by_owner_json_parsed(
        owner, opts={'programId': token_program}, commitment=Confirmed
    )
    balances = []
    if not resp or not resp.value:
        return balances

    for account in resp.value:
        try:
            info = account.account.data.parsed['info']
            mint = info['mint']
            amount_str = info['tokenAmount']['amount']
            decimals = info['tokenAmount']['decimals']
            raw = int(amount_str)
            if raw == 0:
                continue  # skip empty token accounts
            ui = raw / (10 ** decimals)
            # Look up symbol from our metadata cache
            meta = get_cached_metadata(mint)
            symbol = meta['symbol'] if meta else (
                'USDC' if mint == USDC_MINT else '?'
            )
            balances.append(TokenBalance(
                symbol=symbol, mint=mint,
                raw_amount=raw, decimals=decimals, ui_amount=ui,
            ))
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed accounts

    return balances


async def read_balances():
    init_metadata_db()
    console = Console()
    console.print('[bold]Balance Reader[/bold]\\n')

    # Verify wallet first - we don't trust .env until we've confirmed the address
    kp = verify_wallet()
    owner = kp.pubkey()
    console.print()

    console.print(f'Querying Solana RPC: {SOLANA_RPC}')
    console.print(f'Wallet: {owner}\\n')

    async with AsyncClient(SOLANA_RPC) as client:
        try:
            sol = await get_sol_balance(client, owner)
        except Exception as e:
            console.print(f'[red]Failed to fetch SOL balance: {e}[/red]')
            return

        try:
            tokens = await get_token_balances(client, owner)
        except Exception as e:
            console.print(f'[red]Failed to fetch token balances: {e}[/red]')
            tokens = []

    # SOL
    sol_table = Table(title='Wallet Balances')
    sol_table.add_column('Asset')
    sol_table.add_column('Amount', justify='right')
    sol_table.add_column('Mint / Note')

    sol_table.add_row('SOL', f'{sol:.6f}', 'native')

    # USDC + other tokens
    usdc_balance = None
    other_tokens = []
    for t in tokens:
        if t.mint == USDC_MINT:
            usdc_balance = t
        else:
            other_tokens.append(t)

    if usdc_balance:
        sol_table.add_row(
            'USDC', f'{usdc_balance.ui_amount:.4f}',
            'EPjFWd...TDt1v',
        )
    else:
        sol_table.add_row('USDC', '0.0000', '(no USDC token account yet)')

    for t in other_tokens:
        mint_short = f'{t.mint[:6]}...{t.mint[-6:]}'
        sol_table.add_row(t.symbol, f'{t.ui_amount:,.4f}', mint_short)

    console.print(sol_table)

    # Summary
    console.print()
    console.print(f'  SOL: {sol:.6f}')
    if usdc_balance:
        console.print(f'  USDC: {usdc_balance.ui_amount:.4f}')
    console.print(f'  Other token positions: {len(other_tokens)}')


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
