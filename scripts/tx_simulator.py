"""Transaction simulator - Piece 4 of the execution adapter.

Takes a swap template (from Piece 3) and asks Solana RPC: would this work?

Calls simulateTransaction with the unsigned bytes. Solana runs the transaction
against current state and reports:
  - success/failure
  - error message if failure
  - compute units consumed
  - log messages
  - account balance changes (if requested)

Read-only. No signing, no broadcasting, no state changes.

Use this BEFORE Piece 5 (sign and submit) to avoid wasting fees on doomed trades.
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv(dotenv_path=Path('.env'))

HELIUS_KEY = os.getenv('HELIUS_API_KEY')
if not HELIUS_KEY:
    raise SystemExit('ERROR: HELIUS_API_KEY not set in .env')
SOLANA_RPC = f'https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}'


@dataclass
class SimulationResult:
    success: bool
    error: Optional[str]                  # The error from Solana if failed
    error_friendly: Optional[str]         # Human-readable interpretation
    compute_units_consumed: Optional[int]
    logs: list[str]
    raw_response: dict                    # Full RPC response for debugging


# Common Solana errors translated to plain English
ERROR_TRANSLATIONS = {
    'InsufficientFunds': 'Wallet does not have enough SOL/USDC for this trade',
    'InsufficientFundsForRent': 'Not enough SOL to pay account rent + fees',
    'BlockhashNotFound': 'Transaction template expired (blockhash too old) - need a fresh quote',
    'AccountNotFound': 'A required account does not exist (token account may need to be created)',
    'AlreadyProcessed': 'This exact transaction was already submitted',
    'ProgramFailedToComplete': 'A program in the route failed - usually slippage exceeded or DEX issue',
    'Custom': 'Program returned a custom error - check logs for details',
}


def interpret_error(err: any) -> str:
    """Convert a raw Solana error into something a human can read."""
    if err is None:
        return 'No error'
    err_str = str(err)
    for key, friendly in ERROR_TRANSLATIONS.items():
        if key in err_str:
            return friendly
    return f'Unrecognized error: {err_str[:200]}'


async def simulate_transaction(
    client: httpx.AsyncClient,
    transaction_b64: str,
) -> SimulationResult:
    """Simulate a transaction. Returns SimulationResult.

    The transaction passed in is NOT signed yet. Solana's simulator can
    run unsigned transactions if we pass replaceRecentBlockhash=true and
    sigVerify=false - both standard for pre-flight simulation.
    """
    body = {
        'jsonrpc': '2.0', 'id': 1,
        'method': 'simulateTransaction',
        'params': [
            transaction_b64,
            {
                'encoding': 'base64',
                'commitment': 'processed',
                'sigVerify': False,
                'replaceRecentBlockhash': True,
            }
        ]
    }
    r = await client.post(SOLANA_RPC, json=body, timeout=30.0)
    if r.status_code != 200:
        raise RuntimeError(f'Simulate RPC HTTP {r.status_code}: {r.text[:300]}')

    data = r.json()
    if 'error' in data:
        raise RuntimeError(f'Simulate RPC error: {data["error"]}')
    if 'result' not in data:
        raise RuntimeError(f'Simulate response malformed: {data}')

    result = data['result']
    value = result.get('value', {})
    err = value.get('err')

    return SimulationResult(
        success=(err is None),
        error=str(err) if err else None,
        error_friendly=interpret_error(err) if err else None,
        compute_units_consumed=value.get('unitsConsumed'),
        logs=value.get('logs', []) or [],
        raw_response=data,
    )


async def _demo():
    """Demo: build and simulate a $1 USDC -> SOL swap."""
    from scripts.wallet_loader import verify_wallet
    from scripts.jupiter_swap_builder import fetch_quote, build_swap_template

    USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
    SOL_MINT = 'So11111111111111111111111111111111111111112'

    kp = verify_wallet()
    wallet = str(kp.pubkey())
    print()

    async with httpx.AsyncClient() as client:
        print('Fetching quote: $1 USDC -> SOL...')
        quote = await fetch_quote(client, USDC, SOL_MINT,
                                    amount_atomic=1_000_000, slippage_bps=100)

        print('Building swap template...')
        template = await build_swap_template(client, quote, wallet)

        print('Simulating transaction...')
        result = await simulate_transaction(client, template.transaction_b64)
        print()

        if result.success:
            print(f'  Result: SUCCESS')
            print(f'  Compute units: {result.compute_units_consumed}')
            print(f'  Log lines: {len(result.logs)}')
            print()
            print('  Last 5 log lines:')
            for line in result.logs[-5:]:
                print(f'    {line[:120]}')
        else:
            print(f'  Result: FAILED')
            print(f'  Raw error: {result.error}')
            print(f'  Interpretation: {result.error_friendly}')
            print()
            if result.logs:
                print('  Last 10 log lines (clues about why it failed):')
                for line in result.logs[-10:]:
                    print(f'    {line[:120]}')


if __name__ == '__main__':
    asyncio.run(_demo())
