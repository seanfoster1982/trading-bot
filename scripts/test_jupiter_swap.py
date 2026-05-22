import asyncio
import json
import httpx

async def main():
    # Step 1: Get a real quote (we know this works)
    quote_url = "https://lite-api.jup.ag/swap/v1/quote"
    USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    SOL  = "So11111111111111111111111111111111111111112"

    async with httpx.AsyncClient() as client:
        # Quote for $1 USDC -> SOL
        r = await client.get(quote_url, params={
            "inputMint": USDC,
            "outputMint": SOL,
            "amount": "1000000",  # 1 USDC
            "slippageBps": "100",
            "swapMode": "ExactIn",
        }, timeout=20.0)
        if r.status_code != 200:
            print(f"QUOTE FAILED: {r.status_code} {r.text[:200]}")
            return
        quote = r.json()
        print(f"Quote OK: 1 USDC -> {int(quote['outAmount']) / 1e9:.6f} SOL")
        print(f"Route plan steps: {len(quote.get('routePlan', []))}")
        print()

        # Step 2: Get a swap transaction template (this is what Piece 3 will do)
        swap_url = "https://lite-api.jup.ag/swap/v1/swap"
        body = {
            "quoteResponse": quote,
            "userPublicKey": "9SDJkC88PgcaAeqLsbE77nnGUne4ZYZqjDHQbGisfGn3",
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        }
        r = await client.post(swap_url, json=body, timeout=30.0)
        print(f"Swap endpoint HTTP: {r.status_code}")
        if r.status_code != 200:
            print(f"Body: {r.text[:500]}")
            return
        data = r.json()
        tx_b64 = data.get("swapTransaction", "")
        print(f"Got swap transaction template: {len(tx_b64)} chars (base64)")
        print(f"First 100 chars: {tx_b64[:100]}...")
        print(f"Last context slot: {data.get('lastValidBlockHeight', 'n/a')}")
        print(f"Priority fee suggested: {data.get('prioritizationFeeLamports', 'n/a')}")

asyncio.run(main())
