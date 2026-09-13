"""Etherscan FREE read-only client. Does not assume Robinhood 4663 support."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

API_V2 = "https://api.etherscan.io/v2/api"
API_V1 = "https://api.etherscan.io/api"
TIMEOUT = 20.0


def _key() -> str:
    return (os.getenv("ETHERSCAN_API_KEY") or "").strip().strip('"').strip("'")


def configured() -> bool:
    return bool(_key())


def smoke_test(chain_id: int = 1) -> int:
    """Smoke on a commonly supported chain (default Ethereum mainnet=1)."""
    key = _key()
    if not key:
        print("ETHERSCAN: WAITING (ETHERSCAN_API_KEY missing)")
        return 2
    # Prefer v2 with chainid; fall back to v1 eth_blockNumber
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(
                API_V2,
                params={
                    "chainid": str(chain_id),
                    "module": "proxy",
                    "action": "eth_blockNumber",
                    "apikey": key,
                },
            )
            if r.status_code == 404:
                r = client.get(
                    API_V1,
                    params={"module": "proxy", "action": "eth_blockNumber", "apikey": key},
                )
            r.raise_for_status()
            body = r.json()
            result = body.get("result")
            # Never print apikey
            if not result or (isinstance(result, str) and result.lower().startswith("invalid")):
                msg = str(body.get("message") or body.get("result") or "bad response")
                print(f"ETHERSCAN: FAILED {msg[:120]}")
                return 1
            print(f"ETHERSCAN: CONNECTED chain_id={chain_id} block={result}")
            print("NOTE: Do not assume Robinhood chain 4663 is Etherscan-supported.")
            return 0
    except Exception as e:
        print(f"ETHERSCAN: FAILED {type(e).__name__}: {e}")
        return 1


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--smoke-test", action="store_true")
    p.add_argument("--chain-id", type=int, default=1)
    args = p.parse_args()
    if args.smoke_test:
        raise SystemExit(smoke_test(args.chain_id))
    p.print_help()


if __name__ == "__main__":
    main()
