from pathlib import Path

content = """\"\"\"Wallet loader - Piece 1 of the execution adapter.

Read-only. Loads the bot wallet's private key from .env, derives the public
address, and verifies it matches the expected wallet address.

Nothing is signed. Nothing is broadcast. Pure cryptographic derivation.

If the derived address does NOT match EXPECTED_PUBLIC_ADDRESS, this exits with
error. That's the safety check - if the wrong key is in .env, we catch it
before any code touches a transaction.
\"\"\"
from __future__ import annotations

import os
import sys
from pathlib import Path

import base58
from dotenv import load_dotenv
from solders.keypair import Keypair

# The bot wallet we expect SOLANA_PRIVATE_KEY in .env to decode to.
# This is the wallet you created in Phantom on May 12.
# If the derived address differs from this, something is wrong - abort.
EXPECTED_PUBLIC_ADDRESS = 'qfXnLih4u6DExx3Sux2X2gRKwsvDCCcctbsAkL6Hgor'


def load_keypair() -> Keypair:
    \"\"\"Load the bot wallet keypair from SOLANA_PRIVATE_KEY in .env.
    Phantom exports keys as base58-encoded 64-byte arrays.
    Returns a solders Keypair. Raises SystemExit if anything is wrong.
    \"\"\"
    load_dotenv(dotenv_path=Path('.env'))
    key_str = os.getenv('SOLANA_PRIVATE_KEY')
    if not key_str:
        print('ERROR: SOLANA_PRIVATE_KEY not found in .env')
        sys.exit(1)

    # Decode base58 -> bytes
    try:
        key_bytes = base58.b58decode(key_str)
    except Exception as e:
        print(f'ERROR: SOLANA_PRIVATE_KEY is not valid base58: {e}')
        sys.exit(1)

    if len(key_bytes) != 64:
        print(f'ERROR: decoded key is {len(key_bytes)} bytes, expected 64')
        print('  Phantom private keys decode to 64 bytes')
        print('  If yours is 32, you may have only the seed - re-export from Phantom')
        sys.exit(1)

    # Build keypair from bytes
    try:
        kp = Keypair.from_bytes(key_bytes)
    except Exception as e:
        print(f'ERROR: could not construct Keypair from key bytes: {e}')
        sys.exit(1)

    return kp


def verify_wallet():
    \"\"\"Load keypair, derive address, compare to expected. Exits if mismatch.\"\"\"
    print('Loading bot wallet from .env...')
    kp = load_keypair()
    derived = str(kp.pubkey())

    print(f'  Derived public address: {derived}')
    print(f'  Expected public address: {EXPECTED_PUBLIC_ADDRESS}')

    if derived != EXPECTED_PUBLIC_ADDRESS:
        print()
        print('ERROR: MISMATCH - derived address does not match expected.')
        print('This means either:')
        print('  1. The SOLANA_PRIVATE_KEY in .env is for a different wallet')
        print('  2. The EXPECTED_PUBLIC_ADDRESS constant in this script is wrong')
        print('Aborting. No further execution code should be run until this is resolved.')
        sys.exit(1)

    print()
    print('OK: derived address matches expected. Wallet loader verified.')
    return kp


if __name__ == '__main__':
    verify_wallet()
"""

Path('scripts/wallet_loader.py').write_text(content, encoding='utf-8')
print('Wrote scripts/wallet_loader.py')

import ast
try:
    ast.parse(content)
    print('Syntax OK')
except SyntaxError as e:
    print(f'Syntax error: {e}')
