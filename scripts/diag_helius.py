import asyncio
import os
import json
from pathlib import Path
import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(".env"))

KEY = os.getenv("HELIUS_API_KEY")
if not KEY:
    raise SystemExit("HELIUS_API_KEY not set")

URL = f"https://mainnet.helius-rpc.com/?api-key={KEY}"
WALLET = "qfXnLih4u6DExx3Sux2X2gRKwsvDCCcctbsAkL6Hgor"

async def main():
    async with httpx.AsyncClient() as c:
        print("=== getBalance (native SOL) ===")
        r = await c.post(URL, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [WALLET]
        }, timeout=30.0)
        print(f"HTTP {r.status_code}")
        print(json.dumps(r.json(), indent=2))
        print()

        print("=== getTokenAccountsByOwner (SPL tokens) ===")
        r = await c.post(URL, json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "getTokenAccountsByOwner",
            "params": [
                WALLET,
                {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                {"encoding": "jsonParsed"}
            ]
        }, timeout=30.0)
        print(f"HTTP {r.status_code}")
        data = r.json()
        # Show summary not raw firehose
        if "result" in data and "value" in data["result"]:
            accounts = data["result"]["value"]
            print(f"Found {len(accounts)} token accounts")
            for i, acc in enumerate(accounts[:5]):
                parsed = acc["account"]["data"]["parsed"]
                info = parsed.get("info", {})
                mint = info.get("mint")
                amount = info.get("tokenAmount", {}).get("uiAmount")
                print(f"  [{i}] mint={mint} uiAmount={amount}")
        else:
            print(json.dumps(data, indent=2))

asyncio.run(main())
