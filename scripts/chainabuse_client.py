"""Chainabuse FREE limited client with local monthly quota guard."""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
COUNTER = ROOT / "data" / "cache" / "chainabuse_monthly.json"
API = "https://api.chainabuse.com/v0/reports"
TIMEOUT = 20.0


def _key() -> str:
    return (os.getenv("CHAINABUSE_API_KEY") or "").strip().strip('"').strip("'")


def configured() -> bool:
    return bool(_key())


def monthly_cap() -> int:
    try:
        return max(0, int(os.getenv("CHAINABUSE_MONTHLY_CALL_CAP") or "8"))
    except ValueError:
        return 8


def _ym() -> str:
    return time.strftime("%Y-%m")


def _load_counter() -> dict:
    try:
        return json.loads(COUNTER.read_text(encoding="utf-8"))
    except Exception:
        return {"ym": _ym(), "count": 0}


def _save_counter(data: dict) -> None:
    COUNTER.parent.mkdir(parents=True, exist_ok=True)
    COUNTER.write_text(json.dumps(data), encoding="utf-8")


def remaining() -> int:
    data = _load_counter()
    if data.get("ym") != _ym():
        data = {"ym": _ym(), "count": 0}
        _save_counter(data)
    return max(0, monthly_cap() - int(data.get("count") or 0))


def _inc() -> None:
    data = _load_counter()
    if data.get("ym") != _ym():
        data = {"ym": _ym(), "count": 0}
    data["count"] = int(data.get("count") or 0) + 1
    _save_counter(data)


def smoke_test(*, confirm_metered: bool) -> int:
    if not configured():
        print("CHAINABUSE: WAITING (CHAINABUSE_API_KEY missing)")
        return 2
    if not confirm_metered:
        print("CHAINABUSE: SKIPPED (pass --confirm-metered to spend 1 of scarce free quota)")
        print(f"CHAINABUSE: remaining_local_cap={remaining()} / {monthly_cap()}")
        return 0
    if remaining() <= 0:
        print("CHAINABUSE: FAILED local monthly cap exhausted")
        return 1
    key = _key()
    token = base64.b64encode(f"{key}:".encode()).decode()
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(
                API,
                params={"page": 1, "perPage": 1},
                headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
            )
        _inc()
        # Never log key/token
        if r.status_code in (401, 403):
            print(f"CHAINABUSE: FAILED auth HTTP {r.status_code}")
            return 1
        if r.status_code == 429:
            print("CHAINABUSE: FAILED rate limited")
            return 1
        if r.status_code >= 500:
            print(f"CHAINABUSE: FAILED HTTP {r.status_code}")
            return 1
        r.raise_for_status()
        print(f"CHAINABUSE: CONNECTED remaining_local_cap={remaining()}")
        return 0
    except Exception as e:
        print(f"CHAINABUSE: FAILED {type(e).__name__}")
        return 1


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--smoke-test", action="store_true")
    p.add_argument("--confirm-metered", action="store_true")
    args = p.parse_args()
    if args.smoke_test:
        raise SystemExit(smoke_test(confirm_metered=args.confirm_metered))
    p.print_help()


if __name__ == "__main__":
    main()
