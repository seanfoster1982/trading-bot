import asyncio
import os
import json
import httpx
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path('.env'))

BIRDEYE_KEY = os.getenv('BIRDEYE_API_KEY')
BASE = 'https://public-api.birdeye.so'

# TROLL
ADDRESS = '5UUH9RTDiSpq6HKS6bp4NdU9PNJpXRXuiw6ShBTBhgH2'

async def main():
    if not BIRDEYE_KEY:
        print('BIRDEYE_API_KEY not loaded from .env')
        return

    headers = {
        'X-API-KEY': BIRDEYE_KEY,
        'x-chain': 'solana',
        'accept': 'application/json',
    }
    async with httpx.AsyncClient() as client:
        print('=== /defi/v3/token/holder ===')
        r = await client.get(f'{BASE}/defi/v3/token/holder',
                             headers=headers,
                             params={'address': ADDRESS, 'limit': 3, 'offset': 0})
        print(f'Status: {r.status_code}')
        if r.status_code == 200:
            print(json.dumps(r.json(), indent=2)[:2000])
        else:
            print(r.text[:500])

        print()
        print('=== /defi/v3/holder-stats/single ===')
        r = await client.get(f'{BASE}/defi/v3/holder-stats/single',
                             headers=headers,
                             params={'address': ADDRESS})
        print(f'Status: {r.status_code}')
        if r.status_code == 200:
            print(json.dumps(r.json(), indent=2)[:1500])
        else:
            print(r.text[:500])

        print()
        print('=== /defi/v3/token/exit-liquidity ===')
        r = await client.get(f'{BASE}/defi/v3/token/exit-liquidity',
                             headers=headers,
                             params={'address': ADDRESS})
        print(f'Status: {r.status_code}')
        if r.status_code == 200:
            print(json.dumps(r.json(), indent=2)[:1500])
        else:
            print(r.text[:500])

        print()
        print('=== /defi/token_overview (supply/mc reference) ===')
        r = await client.get(f'{BASE}/defi/token_overview',
                             headers=headers,
                             params={'address': ADDRESS})
        print(f'Status: {r.status_code}')
        if r.status_code == 200:
            data = r.json()
            d = data.get('data', {})
            keys_of_interest = ['supply', 'totalSupply', 'mc', 'marketCap',
                                'holder', 'holders', 'circulatingSupply', 'decimals']
            relevant = {k: d.get(k) for k in keys_of_interest if k in d}
            print(json.dumps({'success': data.get('success'), 'relevant_fields': relevant}, indent=2))
            print(f'All keys: {list(d.keys())[:40]}')
        else:
            print(r.text[:500])

asyncio.run(main())
