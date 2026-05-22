import asyncio
import os
from pathlib import Path
import sqlite3
import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(".env"))

BIRDEYE_KEY = os.getenv("BIRDEYE_API_KEY")
BASE = "https://public-api.birdeye.so"

# Get verified addresses straight from the database
conn = sqlite3.connect("data/memecoins.db")
rows = conn.execute(
    "SELECT DISTINCT symbol, address FROM screened_tokens LIMIT 10"
).fetchall()
conn.close()

async def main():
    headers = {"X-API-KEY": BIRDEYE_KEY, "x-chain": "solana", "accept": "application/json"}
    async with httpx.AsyncClient() as client:
        for sym, addr in rows:
            r = await client.get(f"{BASE}/defi/token_overview",
                                 headers=headers, params={"address": addr}, timeout=15.0)
            if r.status_code == 200:
                d = r.json().get("data", {})
                print(f"{sym}: decimals={d.get('decimals')} supply={d.get('totalSupply')}")
            else:
                print(f"{sym}: HTTP {r.status_code}")

asyncio.run(main())
