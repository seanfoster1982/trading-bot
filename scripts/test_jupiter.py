import asyncio
import httpx

async def test_url(url, label):
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, timeout=15.0)
            print(f"{label}: status {r.status_code}")
            if r.status_code == 200:
                preview = r.text[:200]
                print(f"  Preview: {preview}")
            else:
                print(f"  Body: {r.text[:200]}")
    except Exception as e:
        print(f"{label}: FAILED - {type(e).__name__}: {str(e)[:200]}")

async def main():
    USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    SOL  = "So11111111111111111111111111111111111111112"
    amount = 1_000_000

    url1 = f"https://lite-api.jup.ag/swap/v1/quote?inputMint={USDC}&outputMint={SOL}&amount={amount}&slippageBps=100"
    await test_url(url1, "lite-api.jup.ag /swap/v1/quote")

    url2 = f"https://lite-api.jup.ag/v6/quote?inputMint={USDC}&outputMint={SOL}&amount={amount}&slippageBps=100"
    await test_url(url2, "lite-api.jup.ag /v6/quote")

    url3 = f"https://api.jup.ag/swap/v1/quote?inputMint={USDC}&outputMint={SOL}&amount={amount}&slippageBps=100"
    await test_url(url3, "api.jup.ag /swap/v1/quote")

    url4 = f"https://quote-api.jup.ag/v6/quote?inputMint={USDC}&outputMint={SOL}&amount={amount}&slippageBps=100"
    await test_url(url4, "quote-api.jup.ag /v6/quote (deprecated)")

asyncio.run(main())
