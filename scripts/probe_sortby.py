import os, httpx
from dotenv import load_dotenv
load_dotenv()
key = os.getenv("BIRDEYE_API_KEY")
base = "https://public-api.birdeye.so/defi/v3/token/list"
headers = {"X-API-KEY": key, "x-chain": "solana"}

# Try each candidate sort_by value
candidates = [
    "liquidity", "market_cap", "fdv", "recent_listing_time",
    "volume_24h_usd", "volume_24h_change_percent", "price_change_24h_percent",
    "trade_24h_count",
]

for sb in candidates:
    params = {"sort_by": sb, "sort_type": "desc", "limit": 1, "min_liquidity": 100000}
    try:
        r = httpx.get(base, params=params, headers=headers, timeout=10)
        if r.status_code == 200:
            print(f"  OK:  {sb}")
        else:
            print(f"  BAD: {sb}  ({r.status_code}: {r.text[:120]})")
    except Exception as e:
        print(f"  ERR: {sb}  ({e})")
