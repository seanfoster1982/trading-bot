"""Bounded read-only X API adapter. X_ENABLED defaults false. No posts."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
COUNTER = ROOT / "data" / "cache" / "x_usage.json"
TIMEOUT = 20.0


def _token() -> str:
    return (os.getenv("X_BEARER_TOKEN") or "").strip().strip('"').strip("'")


def configured() -> bool:
    return bool(_token())


def enabled() -> bool:
    return (os.getenv("X_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")


def max_per_cycle() -> int:
    try:
        return max(0, int(os.getenv("X_MAX_REQUESTS_PER_CYCLE") or "5"))
    except ValueError:
        return 5


def max_daily() -> int:
    try:
        return max(0, int(os.getenv("X_MAX_DAILY_REQUESTS") or "50"))
    except ValueError:
        return 50


def _usage() -> dict:
    try:
        return json.loads(COUNTER.read_text(encoding="utf-8"))
    except Exception:
        return {"day": time.strftime("%Y-%m-%d"), "count": 0}


def _save(u: dict) -> None:
    COUNTER.parent.mkdir(parents=True, exist_ok=True)
    COUNTER.write_text(json.dumps(u), encoding="utf-8")


def daily_remaining() -> int:
    u = _usage()
    if u.get("day") != time.strftime("%Y-%m-%d"):
        u = {"day": time.strftime("%Y-%m-%d"), "count": 0}
        _save(u)
    return max(0, max_daily() - int(u.get("count") or 0))


def _inc() -> None:
    u = _usage()
    if u.get("day") != time.strftime("%Y-%m-%d"):
        u = {"day": time.strftime("%Y-%m-%d"), "count": 0}
    u["count"] = int(u.get("count") or 0) + 1
    _save(u)


def recent_search(query: str, max_results: int = 10) -> dict:
    if not configured():
        raise RuntimeError("X_BEARER_TOKEN missing")
    if daily_remaining() <= 0:
        raise RuntimeError("X daily request cap reached")
    headers = {"Authorization": f"Bearer {_token()}"}
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.get(
            "https://api.twitter.com/2/tweets/search/recent",
            params={"query": query, "max_results": max(10, min(max_results, 100))},
            headers=headers,
        )
    _inc()
    if r.status_code == 401:
        raise RuntimeError("X auth failed")
    if r.status_code == 429:
        raise RuntimeError("X rate limited")
    if r.status_code >= 500:
        raise RuntimeError(f"X HTTP {r.status_code}")
    r.raise_for_status()
    return r.json()


def smoke_test() -> int:
    if not configured():
        print("X: WAITING (X_BEARER_TOKEN missing)")
        return 2
    print(
        f"X: configured YES enabled={enabled()} "
        f"daily_remaining={daily_remaining()} max_cycle={max_per_cycle()}"
    )
    print("X: CONNECTED (credential present; live decision input DISABLED)")
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--smoke-test", action="store_true")
    p.add_argument("--search", default="")
    args = p.parse_args()
    if args.smoke_test:
        raise SystemExit(smoke_test())
    if args.search:
        if not enabled():
            print("X_ENABLED=false — refusing search")
            raise SystemExit(1)
        data = recent_search(args.search)
        print("X search keys:", list(data.keys()))
        return
    p.print_help()


if __name__ == "__main__":
    main()
