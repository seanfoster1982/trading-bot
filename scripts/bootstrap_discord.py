"""Discord prep helper — channel plan + env presence. No secrets printed."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CHANNELS = [
    "#operations",
    "#trade-executions",
    "#signals",
    "#risk-vetoes",
    "#security",
    "#airdrops",
    "#market-intelligence",
    "#cursor-dev",
    "#system-health",
    "#audit-log",
]

ENV_NAMES = [
    "DISCORD_ENABLED",
    "DISCORD_BOT_TOKEN",
    "DISCORD_APPLICATION_ID",
    "DISCORD_GUILD_ID",
    "DISCORD_OPERATOR_ID",
    "DISCORD_WEBHOOK_URL",
]


def _present(name: str) -> bool:
    return bool((os.getenv(name) or "").strip())


def check() -> int:
    print("DISCORD PREP (control PAUSED until stop-control)")
    print("Planned channels:")
    for c in CHANNELS:
        print(" ", c)
    print("Env presence (values hidden):")
    for n in ENV_NAMES:
        if n == "DISCORD_ENABLED":
            raw = (os.getenv(n) or "").strip().lower()
            print(f"  {n}: set={bool(raw)} enabled={raw in ('1','true','yes','on')}")
        else:
            print(f"  {n}: {'YES' if _present(n) else 'NO'}")
    print("DISCORD: READY_FOR_CREDS" if _present("DISCORD_BOT_TOKEN") else "DISCORD: WAITING")
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true")
    args = p.parse_args()
    raise SystemExit(check())


if __name__ == "__main__":
    main()
