"""Configure Telegram phone alerts from .env. Never prints secrets.

Usage:
    python scripts/setup_telegram.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from telegram_notifier import setup  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(0 if setup(send_test=True) else 1)
