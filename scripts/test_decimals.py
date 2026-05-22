import asyncio
import os
import json
from pathlib import Path
import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(".env"))

BIRDEYE_KEY = os.getenv("BIRDEYE_API_KEY")
BASE = "https://public-api.birdeye.so"

# Test on three different tokens to confirm decimals field is reliable
TOKENS = [
    ("TROLL", "5UUH9RTDiSpq6HKS6bp4NdU9PNJpXRXuiw6ShBTBhgH2"),
    ("ASTEROID", "4UeLCRqAxc83HEAVUNwRPnrxAceM5BCGEvL68wA4huBAGS"),
    ("BULL", "3TYgKwkEjs1cVgU2AVgGuKL1NF8nP2X3eCwYr5pJHspump"),
]

async def main():
    headers = {"X-API-KEY": BIRDEYE_KEY, "x-chain": "solana", "accept": "application/json"}
    async with httpx.AsyncClient() as client:
        for sym, addr in TOKENS:
            r = await client.get(f"{BASE}/defi/token_overview",
                                 headers=headers, params={"address": addr}, timeout=15.0)
            if r.status_code == 200:
                d = r.json().get("data", {})
                print(f"{sym}: decimals={d.get('decimals')} supply={d.get('totalSupply')} mc={d.get('marketCap')}")
            else:
                print(f"{sym}: HTTP {r.status_code}")

asyncio.run(main())
