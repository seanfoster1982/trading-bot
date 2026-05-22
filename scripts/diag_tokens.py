import asyncio, os, json
from pathlib import Path
import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(".env"))
KEY = os.getenv("HELIUS_API_KEY")
URL = f"https://mainnet.helius-rpc.com/?api-key={KEY}"
WALLET = "9SDJkC88PgcaAeqLsbE77nnGUne4ZYZqjDHQbGisfGn3"

async def main():
    async with httpx.AsyncClient() as c:
        r = await c.post(URL, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                WALLET,
                {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                {"encoding": "jsonParsed"}
            ]
        }, timeout=30.0)
        data = r.json()
        if "result" not in data:
            print("UNEXPECTED RESPONSE:")
            print(json.dumps(data, indent=2))
            return
        accounts = data["result"]["value"]
        print(f"Found {len(accounts)} SPL token accounts on {WALLET}")
        print()
        for i, acc in enumerate(accounts):
            info = acc["account"]["data"]["parsed"]["info"]
            mint = info["mint"]
            amount = info["tokenAmount"]["uiAmount"]
            decimals = info["tokenAmount"]["decimals"]
            raw = info["tokenAmount"]["amount"]
            print(f"  [{i}] mint={mint}")
            print(f"      uiAmount={amount}  raw={raw}  decimals={decimals}")

asyncio.run(main())
