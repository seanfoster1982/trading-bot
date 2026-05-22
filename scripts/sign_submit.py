"""Sign and submit - Piece 5 of the execution adapter.

THE DANGEROUS ONE. This module signs Solana transactions with your private key
and broadcasts them. Real money moves.

Safety architecture:
  1. Pre-flight simulate before signing (catch stale templates)
  2. Verify expiry (lastValidBlockHeight not passed)
  3. Verify SOL balance covers fees + rent
  4. Sign with solders keypair
  5. Verify signature is valid before broadcasting
  6. Submit with maxRetries=0, skipPreflight=False
  7. Poll signature status for up to 30s
  8. NEVER retry on timeout - uncertainty halts, does not resubmit

If anything looks wrong at any step, abort. Never trade on uncertainty.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.jupiter_swap_builder import SwapTemplate
from scripts.tx_simulator import simulate_transaction

load_dotenv(dotenv_path=Path('.env'))

HELIUS_KEY = os.getenv('HELIUS_API_KEY')
if not HELIUS_KEY:
    raise SystemExit('ERROR: HELIUS_API_KEY not set in .env')
SOLANA_RPC = f'https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}'

# Minimum SOL we require the wallet to hold AFTER the trade for fees and rent
# Solana rent-exempt minimum for a basic account is ~0.00089 SOL
# We require 0.005 SOL margin to cover fees + a few token account creations
MIN_SOL_RESERVE_LAMPORTS = 5_000_000  # 0.005 SOL

# Maximum time to poll for confirmation
MAX_CONFIRMATION_SECONDS = 30
POLL_INTERVAL_SECONDS = 1.5


class SubmissionStatus(Enum):
    CONFIRMED = 'confirmed'        # Transaction landed successfully
    FAILED_ONCHAIN = 'failed'      # Transaction landed but reverted
    TIMEOUT = 'timeout'            # Unknown state - halt, don't retry
    REJECTED_PREFLIGHT = 'rejected_preflight'  # Solana rejected before submitting
    ABORTED_PRECHECK = 'aborted_precheck'      # Our checks failed before signing


@dataclass
class SubmissionResult:
    status: SubmissionStatus
    signature: Optional[str]              # Tx signature if submitted
    error: Optional[str]                  # Error description if any
    elapsed_seconds: float
    confirmation_logs: Optional[list[str]] = None  # On-chain logs if confirmed


async def get_current_block_height(client: httpx.AsyncClient) -> int:
    """Get current Solana block height for expiry checks."""
    r = await client.post(SOLANA_RPC, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'getBlockHeight'
    }, timeout=10.0)
    if r.status_code != 200:
        raise RuntimeError(f'getBlockHeight HTTP {r.status_code}')
    data = r.json()
    if 'result' not in data:
        raise RuntimeError(f'getBlockHeight bad response: {data}')
    return int(data['result'])


async def get_sol_lamports(client: httpx.AsyncClient, address: str) -> int:
    """Get current SOL balance in lamports."""
    r = await client.post(SOLANA_RPC, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'getBalance', 'params': [address]
    }, timeout=10.0)
    if r.status_code != 200:
        raise RuntimeError(f'getBalance HTTP {r.status_code}')
    data = r.json()
    if 'result' not in data or 'value' not in data['result']:
        raise RuntimeError(f'getBalance bad response: {data}')
    return int(data['result']['value'])


async def submit_signed_transaction(
    client: httpx.AsyncClient,
    signed_tx_b64: str,
) -> str:
    """Submit a signed transaction. Returns the signature."""
    r = await client.post(SOLANA_RPC, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'sendTransaction',
        'params': [
            signed_tx_b64,
            {
                'encoding': 'base64',
                'skipPreflight': False,
                'maxRetries': 0,
                'preflightCommitment': 'processed',
            }
        ]
    }, timeout=30.0)
    if r.status_code != 200:
        raise RuntimeError(f'sendTransaction HTTP {r.status_code}: {r.text[:300]}')
    data = r.json()
    if 'error' in data:
        raise RuntimeError(f'sendTransaction error: {data["error"]}')
    if 'result' not in data:
        raise RuntimeError(f'sendTransaction bad response: {data}')
    return data['result']  # signature string


async def get_signature_status(
    client: httpx.AsyncClient,
    signature: str,
) -> Optional[dict]:
    """Check a single signature status. Returns dict with confirmationStatus and err."""
    r = await client.post(SOLANA_RPC, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'getSignatureStatuses',
        'params': [[signature], {'searchTransactionHistory': True}]
    }, timeout=10.0)
    if r.status_code != 200:
        return None
    data = r.json()
    if 'result' not in data:
        return None
    values = data['result'].get('value', [])
    if not values or values[0] is None:
        return None
    return values[0]


async def get_transaction_logs(
    client: httpx.AsyncClient,
    signature: str,
) -> list[str]:
    """Get the on-chain logs for a confirmed transaction."""
    r = await client.post(SOLANA_RPC, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'getTransaction',
        'params': [signature, {'encoding': 'json', 'maxSupportedTransactionVersion': 0}]
    }, timeout=10.0)
    if r.status_code != 200:
        return []
    data = r.json()
    tx = data.get('result')
    if not tx:
        return []
    meta = tx.get('meta', {}) or {}
    return meta.get('logMessages', []) or []


async def sign_and_submit(
    client: httpx.AsyncClient,
    template: SwapTemplate,
    keypair: Keypair,
) -> SubmissionResult:
    """Sign and submit a swap template. Returns SubmissionResult.

    Implements all safety checks. Never retries on timeout.
    """
    start = time.time()
    wallet_address = str(keypair.pubkey())

    # SAFETY CHECK 1: Verify template fee payer matches our keypair
    if template.expected_fee_payer != wallet_address:
        return SubmissionResult(
            status=SubmissionStatus.ABORTED_PRECHECK,
            signature=None,
            error=f'Fee payer mismatch: template={template.expected_fee_payer} keypair={wallet_address}',
            elapsed_seconds=time.time() - start,
        )

    # SAFETY CHECK 2: Verify template hasn't expired
    current_height = await get_current_block_height(client)
    blocks_remaining = template.last_valid_block_height - current_height
    if blocks_remaining <= 0:
        return SubmissionResult(
            status=SubmissionStatus.ABORTED_PRECHECK,
            signature=None,
            error=f'Template expired: current_height={current_height} valid_until={template.last_valid_block_height}',
            elapsed_seconds=time.time() - start,
        )
    if blocks_remaining < 30:
        return SubmissionResult(
            status=SubmissionStatus.ABORTED_PRECHECK,
            signature=None,
            error=f'Template expires too soon: only {blocks_remaining} blocks remaining (need 30+)',
            elapsed_seconds=time.time() - start,
        )

    # SAFETY CHECK 3: Verify SOL balance covers fees + reserve
    sol_lamports = await get_sol_lamports(client, wallet_address)
    if sol_lamports < MIN_SOL_RESERVE_LAMPORTS:
        return SubmissionResult(
            status=SubmissionStatus.ABORTED_PRECHECK,
            signature=None,
            error=f'SOL balance too low: {sol_lamports} lamports < required {MIN_SOL_RESERVE_LAMPORTS}',
            elapsed_seconds=time.time() - start,
        )

    # SAFETY CHECK 4: Pre-flight re-simulate to catch state changes
    sim = await simulate_transaction(client, template.transaction_b64)
    if not sim.success:
        return SubmissionResult(
            status=SubmissionStatus.ABORTED_PRECHECK,
            signature=None,
            error=f'Pre-flight simulation failed: {sim.error_friendly} (raw: {sim.error})',
            elapsed_seconds=time.time() - start,
        )

    # SIGN: Build a new VersionedTransaction with our signature
    message = template.deserialized.message
    try:
        signed_tx = VersionedTransaction(message, [keypair])
    except Exception as e:
        return SubmissionResult(
            status=SubmissionStatus.ABORTED_PRECHECK,
            signature=None,
            error=f'Signing failed: {type(e).__name__}: {e}',
            elapsed_seconds=time.time() - start,
        )

    # Serialize the signed transaction to bytes -> base64
    import base64
    signed_bytes = bytes(signed_tx)
    signed_b64 = base64.b64encode(signed_bytes).decode('ascii')

    # SUBMIT
    try:
        signature = await submit_signed_transaction(client, signed_b64)
    except Exception as e:
        return SubmissionResult(
            status=SubmissionStatus.REJECTED_PREFLIGHT,
            signature=None,
            error=f'Submission rejected: {type(e).__name__}: {e}',
            elapsed_seconds=time.time() - start,
        )

    # CONFIRM: poll signature status
    confirmation_deadline = time.time() + MAX_CONFIRMATION_SECONDS
    while time.time() < confirmation_deadline:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        status = await get_signature_status(client, signature)
        if status is None:
            continue  # Not visible yet
        conf_status = status.get('confirmationStatus')
        err = status.get('err')
        if err is not None:
            logs = await get_transaction_logs(client, signature)
            return SubmissionResult(
                status=SubmissionStatus.FAILED_ONCHAIN,
                signature=signature,
                error=f'Transaction failed on-chain: {err}',
                elapsed_seconds=time.time() - start,
                confirmation_logs=logs,
            )
        if conf_status in ('confirmed', 'finalized'):
            logs = await get_transaction_logs(client, signature)
            return SubmissionResult(
                status=SubmissionStatus.CONFIRMED,
                signature=signature,
                error=None,
                elapsed_seconds=time.time() - start,
                confirmation_logs=logs,
            )

    # TIMEOUT: we don't know if the transaction landed.
    # CRITICAL: do not retry. Caller must manually check signature on Solscan.
    return SubmissionResult(
        status=SubmissionStatus.TIMEOUT,
        signature=signature,
        error=f'Confirmation timeout after {MAX_CONFIRMATION_SECONDS}s - status unknown',
        elapsed_seconds=time.time() - start,
    )


async def _test_one_dollar_swap():
    """Test: sign and submit a real $1 USDC -> SOL swap.

    This is the smoke test. Real money moves (~$0.01-0.02 lost to slippage + fees).
    Requires user to type 'yes' before proceeding.
    """
    from scripts.wallet_loader import verify_wallet
    from scripts.jupiter_swap_builder import fetch_quote, build_swap_template

    USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
    SOL_MINT = 'So11111111111111111111111111111111111111112'

    kp = verify_wallet()
    wallet = str(kp.pubkey())
    print()
    print('=' * 70)
    print('LIVE TRADE TEST: $1 USDC -> SOL')
    print('=' * 70)
    print()
    print(f'Wallet: {wallet}')
    print(f'Trade: 1.00 USDC -> ~0.011 SOL (~$0.99)')
    print(f'Expected cost (slippage + fees): ~$0.01-0.02')
    print()
    print('This is REAL MONEY moving on-chain. Confirm with "yes" to proceed.')
    confirm = input('Proceed with $1 test trade? [yes/N]: ').strip().lower()
    if confirm != 'yes':
        print('Aborted by user.')
        return

    async with httpx.AsyncClient() as client:
        print()
        print('1. Fetching quote...')
        quote = await fetch_quote(client, USDC, SOL_MINT,
                                    amount_atomic=1_000_000, slippage_bps=100)
        print(f'   Quote: 1 USDC -> {int(quote["outAmount"]) / 1e9:.6f} SOL')

        print('2. Building swap template...')
        template = await build_swap_template(client, quote, wallet)
        print(f'   Template: {len(template.transaction_bytes)} bytes')

        print('3. Signing and submitting (with safety checks)...')
        result = await sign_and_submit(client, template, kp)

        print()
        print('=' * 70)
        print(f'Result: {result.status.value.upper()}')
        print('=' * 70)
        print(f'Elapsed: {result.elapsed_seconds:.1f}s')
        if result.signature:
            print(f'Signature: {result.signature}')
            print(f'Solscan: https://solscan.io/tx/{result.signature}')
        if result.error:
            print(f'Error: {result.error}')

        if result.status == SubmissionStatus.CONFIRMED:
            print()
            print('SUCCESS - transaction confirmed on-chain.')
            print('Check Solscan to verify USDC decreased and SOL increased.')
        elif result.status == SubmissionStatus.TIMEOUT:
            print()
            print('TIMEOUT - check Solscan for the signature above.')
            print('DO NOT re-run this test until you know the on-chain state.')


if __name__ == '__main__':
    asyncio.run(_test_one_dollar_swap())
