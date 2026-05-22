import asyncio
import base64
import json
import httpx
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey

async def main():
    quote_url = "https://lite-api.jup.ag/swap/v1/quote"
    swap_url = "https://lite-api.jup.ag/swap/v1/swap"
    USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    SOL  = "So11111111111111111111111111111111111111112"
    WALLET = "9SDJkC88PgcaAeqLsbE77nnGUne4ZYZqjDHQbGisfGn3"

    async with httpx.AsyncClient() as client:
        # Get quote
        r = await client.get(quote_url, params={
            "inputMint": USDC, "outputMint": SOL,
            "amount": "1000000", "slippageBps": "100",
        }, timeout=20.0)
        quote = r.json()
        print("=== QUOTE RESPONSE (key fields) ===")
        print(f"  inAmount: {quote.get('inAmount')}")
        print(f"  outAmount: {quote.get('outAmount')}")
        print(f"  otherAmountThreshold: {quote.get('otherAmountThreshold')}")
        print(f"  swapMode: {quote.get('swapMode')}")
        print(f"  slippageBps: {quote.get('slippageBps')}")
        print(f"  priceImpactPct: {quote.get('priceImpactPct')}")
        print(f"  contextSlot: {quote.get('contextSlot')}")
        print(f"  routePlan length: {len(quote.get('routePlan', []))}")
        for i, step in enumerate(quote.get('routePlan', [])):
            info = step.get('swapInfo', {})
            print(f"    Step {i}: {info.get('label')} ({info.get('ammKey', '')[:20]}...)")
        print()

        # Get swap transaction
        r = await client.post(swap_url, json={
            "quoteResponse": quote,
            "userPublicKey": WALLET,
            "wrapAndUnwrapSol": True,
        }, timeout=30.0)
        data = r.json()
        tx_b64 = data["swapTransaction"]
        tx_bytes = base64.b64decode(tx_b64)

        print(f"=== SWAP TRANSACTION ===")
        print(f"  Base64 length: {len(tx_b64)} chars")
        print(f"  Decoded byte length: {len(tx_bytes)} bytes")
        print(f"  lastValidBlockHeight: {data.get('lastValidBlockHeight')}")
        print()

        # Try to deserialize - this confirms it's a real transaction
        try:
            tx = VersionedTransaction.from_bytes(tx_bytes)
            msg = tx.message
            print(f"  Deserialize: OK")
            print(f"  Num signatures required: {msg.header.num_required_signatures}")
            print(f"  Num readonly signed accounts: {msg.header.num_readonly_signed_accounts}")
            print(f"  Num readonly unsigned accounts: {msg.header.num_readonly_unsigned_accounts}")
            print(f"  Account keys count: {len(msg.account_keys)}")
            print(f"  Instructions count: {len(msg.instructions)}")
            print(f"  Recent blockhash: {msg.recent_blockhash}")
            print()
            print(f"  First account key (should be our wallet): {msg.account_keys[0]}")
            print(f"  Expected wallet:                          {WALLET}")
            match = str(msg.account_keys[0]) == WALLET
            print(f"  Match: {'YES' if match else 'NO - SOMETHING IS WRONG'}")
        except Exception as e:
            print(f"  Deserialize FAILED: {type(e).__name__}: {e}")

asyncio.run(main())
