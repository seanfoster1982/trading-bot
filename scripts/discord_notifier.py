"""Discord notifier stub — DISCORD_ENABLED must remain false until stop-control."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Future read-only commands (NOT activated): /status /positions /pnl /risk /signals /sources
# Do NOT register HALT/RESUME/EMERGENCY_STOP here.


def enabled() -> bool:
    return (os.getenv("DISCORD_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")


def configured() -> bool:
    return bool((os.getenv("DISCORD_BOT_TOKEN") or "").strip())


def send_message(content: str) -> bool:
    if not enabled() or not configured():
        return False
    raise RuntimeError("Discord send disabled until stop-control Phase 2B + explicit enable")
