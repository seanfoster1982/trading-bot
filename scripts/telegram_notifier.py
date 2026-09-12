"""Telegram notifier — phone alerts from .env credentials.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the repo .env.
If either is missing, send() no-ops so callers can fire-and-forget.

Never logs the token or chat id.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _creds() -> tuple[str, str]:
    load_dotenv(dotenv_path=_ENV_PATH)
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat


def is_configured() -> bool:
    token, chat = _creds()
    return bool(token and chat)


def send(text: str, silent: bool = False) -> bool:
    """Send a Telegram message. Returns True on success, False on failure."""
    token, chat = _creds()
    if not token or not chat:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    base = {
        "chat_id": chat,
        "text": text,
        "disable_notification": silent,
        "disable_web_page_preview": True,
    }
    payloads = [{**base, "parse_mode": "HTML"}, dict(base)]
    last_status = None
    for body in payloads:
        try:
            r = httpx.post(url, json=body, timeout=10.0)
        except Exception as e:
            print(f"[telegram] send failed: {type(e).__name__}", file=sys.stderr)
            return False
        last_status = r.status_code
        if r.status_code == 200:
            return True
        if r.status_code != 400:
            print(f"[telegram] HTTP {r.status_code}", file=sys.stderr)
            return False
    print(f"[telegram] HTTP {last_status}", file=sys.stderr)
    return False


send_message = send


def discover_chat_id(token: str) -> str | None:
    """Return the most recent private/group chat id that messaged this bot."""
    token = (token or "").strip()
    if not token:
        return None
    try:
        r = httpx.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            timeout=15.0,
        )
    except Exception as e:
        print(f"[telegram] getUpdates failed: {type(e).__name__}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"[telegram] getUpdates HTTP {r.status_code}", file=sys.stderr)
        return None
    try:
        data = r.json()
    except json.JSONDecodeError:
        return None
    for upd in reversed(data.get("result") or []):
        msg = upd.get("message") or upd.get("my_chat_member") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is not None:
            return str(cid)
    return None


def upsert_env(key: str, value: str, path: Path | None = None) -> None:
    """Set KEY=value in .env without printing the value. Creates the file if needed."""
    path = path or _ENV_PATH
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    prefix = f"{key}="
    found = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(prefix) or stripped.startswith(f"{key} ="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _print_token_help() -> None:
    print("Telegram phone alerts need TELEGRAM_BOT_TOKEN in .env (gitignored).")
    print("Do not paste the token in chat.")
    print("1. Phone: open Telegram, search @BotFather")
    print("2. Reuse @RealChainTradingBot (/token) or /newbot")
    print("3. Add this line to .env:")
    print("     TELEGRAM_BOT_TOKEN=<paste token here>")
    print("4. Open the bot in Telegram and tap Start (send any message)")
    print("5. Re-run: python scripts/setup_telegram.py")


def setup(*, send_test: bool = True) -> bool:
    """Fill TELEGRAM_CHAT_ID from getUpdates if needed, then send a test ping."""
    token, chat = _creds()
    if not token:
        _print_token_help()
        return False
    if not chat:
        print("TELEGRAM_CHAT_ID missing — asking Telegram for the chat that pinged the bot.")
        found = discover_chat_id(token)
        if not found:
            print("No chat yet. Open Telegram, message your bot, then re-run this script.")
            return False
        upsert_env("TELEGRAM_CHAT_ID", found)
        os.environ["TELEGRAM_CHAT_ID"] = found
        print("Saved TELEGRAM_CHAT_ID to .env")
        chat = found
    if not send_test:
        return True
    ok = send(
        "<b>Trading bot alerts are on</b>\n"
        "You will get a phone ping on Robinhood Chain buys, sells, and halts, "
        "plus Solana paper/shadow opens and the 9am/9pm digest."
    )
    if ok:
        print("Test ping sent — check your phone.")
        return True
    print("Token/chat present but Telegram rejected the send. Check the token and chat id.")
    return False


def _test() -> None:
    print(f"Configured: {is_configured()}")
    raise SystemExit(0 if setup(send_test=True) else 1)


if __name__ == "__main__":
    raise SystemExit(0 if setup(send_test=True) else 1)
