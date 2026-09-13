"""DefiLlama FREE read-only client (httpx). No API key. No trades."""
from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx

BASE = "https://api.llama.fi"
TIMEOUT = 20.0


def fetch_protocols(client: httpx.Client | None = None) -> list[dict[str, Any]]:
    own = client is None
    c = client or httpx.Client(timeout=TIMEOUT)
    try:
        r = c.get(f"{BASE}/protocols")
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError("malformed protocols response")
        return data
    finally:
        if own:
            c.close()


def smoke_test() -> int:
    try:
        rows = fetch_protocols()
        if not rows:
            print("DEFILLAMA_FREE: FAILED empty")
            return 1
        sample = rows[0]
        name = sample.get("name") or "?"
        print(f"DEFILLAMA_FREE: CONNECTED protocols={len(rows)} sample={name}")
        return 0
    except Exception as e:
        print(f"DEFILLAMA_FREE: FAILED {type(e).__name__}: {e}")
        return 1


def main() -> None:
    p = argparse.ArgumentParser(description="DefiLlama free read-only client")
    p.add_argument("--smoke-test", action="store_true")
    args = p.parse_args()
    if args.smoke_test:
        raise SystemExit(smoke_test())
    p.print_help()


if __name__ == "__main__":
    main()
