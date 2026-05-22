"""Jupiter swap builder - Piece 3 of the execution adapter.

Calls Jupiter /swap to convert a quote into a transaction template.
Verifies the template is well-formed and addressed to our wallet.

Read-only from a wallet perspective: does NOT sign, does NOT broadcast.
Output: bytes that, if signed, would execute the swap. We can also
inspect the bytes to see exactly what the transaction would do.

The transaction has a short lifetime - it must be signed and submitted
within ~60 seconds before its recent_blockhash expires.
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
from solders.transaction import VersionedTransaction

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv(dotenv_path=Path('.env'))

JUPITER_QUOTE_URL = 'https://lite-api.jup.ag/swap/v1/quote'
JUPITER_SWAP_URL  = 'https://lite-api.jup.ag/swap/v1/swap'


@dataclass
class SwapTemplate:
    """A swap transaction ready to be signed and submitted."""
    transaction_bytes: bytes              # The raw transaction (not yet signed)
    transaction_b64: str                  # Same, base64-encoded
    deserialized: VersionedTransaction    # Parsed transaction for inspection
    expected_fee_payer: str               # Wallet address that must sign (us)
    last_valid_block_height: int          # When this template expires
    priority_fee_lamports: int            # Suggested priority fee from Jupiter
    quote_in_amount: int                  # Input amount in atomic units
    quote_out_amount: int                 # Expected output in atomic units
    quote_min_out_amount: int             # Min output after slippage (otherAmountThreshold)
    quote_route_steps: int                # Number of DEXes in the route
    quote_price_impact_pct: float


async def fetch_quote(
    client: httpx.AsyncClient,
    input_mint: str,
    output_mint: str,
    amount_atomic: int,
    slippage_bps: int,
) -> dict:
    """Fetch a quote from Jupiter. Returns the raw quote dict to pass to /swap."""
    r = await client.get(JUPITER_QUOTE_URL, params={
        'inputMint': input_mint,
        'outputMint': output_mint,
        'amount': str(amount_atomic),
        'slippageBps': str(slippage_bps),
        'swapMode': 'ExactIn',
    }, timeout=20.0)
    if r.status_code != 200:
        raise RuntimeError(f'Quote HTTP {r.status_code}: {r.text[:300]}')
    quote = r.json()
    if 'outAmount' not in quote or 'routePlan' not in quote:
        raise RuntimeError(f'Quote response malformed: {quote}')
    return quote


async def build_swap_template(
    client: httpx.AsyncClient,
    quote: dict,
    user_public_key: str,
) -> SwapTemplate:
    """Call Jupiter /swap to get a swap transaction template.

    Verifies the returned transaction:
      1. Deserializes correctly
      2. Names our wallet as the fee payer (first account key)
      3. Has a recent blockhash + expiry info
    """
    body = {
        'quoteResponse': quote,
        'userPublicKey': user_public_key,
        'wrapAndUnwrapSol': True,
        'dynamicComputeUnitLimit': True,
        'prioritizationFeeLamports': 'auto',
    }
    r = await client.post(JUPITER_SWAP_URL, json=body, timeout=30.0)
    if r.status_code != 200:
        raise RuntimeError(f'Swap HTTP {r.status_code}: {r.text[:500]}')
    data = r.json()
    if 'swapTransaction' not in data:
        raise RuntimeError(f'Swap response missing swapTransaction: {data}')

    tx_b64 = data['swapTransaction']
    tx_bytes = base64.b64decode(tx_b64)

    # Deserialize and verify - this is the safety check
    try:
        tx = VersionedTransaction.from_bytes(tx_bytes)
    except Exception as e:
        raise RuntimeError(f'Transaction failed to deserialize: {e}')

    if len(tx.message.account_keys) == 0:
        raise RuntimeError('Transaction has zero account keys - malformed')

    fee_payer = str(tx.message.account_keys[0])
    if fee_payer != user_public_key:
        raise RuntimeError(
            f'Transaction fee payer mismatch!\n'
            f'  Expected: {user_public_key}\n'
            f'  Got: {fee_payer}\n'
            f'  This means Jupiter built a transaction for the wrong wallet. '
            f'Aborting.'
        )

    last_valid_block_height = data.get('lastValidBlockHeight')
    if last_valid_block_height is None:
        raise RuntimeError('Swap response missing lastValidBlockHeight')

    priority_fee = int(data.get('prioritizationFeeLamports', 0) or 0)

    try:
        price_impact = float(quote.get('priceImpactPct', 0) or 0)
    except (TypeError, ValueError):
        price_impact = 0.0

    return SwapTemplate(
        transaction_bytes=tx_bytes,
        transaction_b64=tx_b64,
        deserialized=tx,
        expected_fee_payer=fee_payer,
        last_valid_block_height=int(last_valid_block_height),
        priority_fee_lamports=priority_fee,
        quote_in_amount=int(quote['inAmount']),
        quote_out_amount=int(quote['outAmount']),
        quote_min_out_amount=int(quote['otherAmountThreshold']),
        quote_route_steps=len(quote.get('routePlan', [])),
        quote_price_impact_pct=price_impact,
    )


async def _demo():
    """Demo: build a $1 USDC -> SOL swap template and print its details."""
    from scripts.wallet_loader import verify_wallet

    USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
    SOL_MINT = 'So11111111111111111111111111111111111111112'

    kp = verify_wallet()
    wallet = str(kp.pubkey())
    print()

    async with httpx.AsyncClient() as client:
        print('Fetching $1 USDC -> SOL quote...')
        quote = await fetch_quote(client, USDC, SOL_MINT,
                                    amount_atomic=1_000_000, slippage_bps=100)
        print(f'  Out: {int(quote["outAmount"]) / 1e9:.6f} SOL')
        print(f'  Route steps: {len(quote.get("routePlan", []))}')
        print()

        print('Building swap transaction template...')
        template = await build_swap_template(client, quote, wallet)

        print(f'  Template size: {len(template.transaction_bytes)} bytes')
        print(f'  Fee payer: {template.expected_fee_payer}')
        print(f'  Last valid block height: {template.last_valid_block_height}')
        print(f'  Priority fee: {template.priority_fee_lamports} lamports')
        print(f'  Account keys: {len(template.deserialized.message.account_keys)}')
        print(f'  Instructions: {len(template.deserialized.message.instructions)}')
        print()
        print('Verification: fee payer matches our wallet, transaction deserialized')
        print('Ready to be passed to Piece 4 (simulator) or Piece 5 (signer)')


if __name__ == '__main__':
    asyncio.run(_demo())
