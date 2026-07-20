"""Telegram notifier - sends bot alerts to your phone.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env.
If either is missing, the module no-ops (does nothing instead of erroring).

This lets the rest of the system call send() unconditionally - if Telegram
isn't configured, nothing happens. If it is, you get a push notification.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

# Resolve .env relative to the repo root (this file's grandparent), not the
# current working directory. The scheduled task launches from System32, so a
# bare Path('.env') would silently fail to load the Telegram credentials.
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / '.env')

_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')


def is_configured() -> bool:
    return bool(_TOKEN and _CHAT_ID)


def send(text: str, silent: bool = False) -> bool:
    """Send a Telegram message. Returns True on success, False on failure.

    If TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are not set, returns False
    without raising. This allows callers to send unconditionally.

    silent=True suppresses the notification sound but still delivers the message.
    """
    if not is_configured():
        return False
    try:
        url = f'https://api.telegram.org/bot{_TOKEN}/sendMessage'
        body = {
            'chat_id': _CHAT_ID,
            'text': text,
            'disable_notification': silent,
            'parse_mode': 'HTML',
        }
        r = httpx.post(url, json=body, timeout=10.0)
        if r.status_code != 200:
            print(f'[telegram] HTTP {r.status_code}: {r.text[:200]}', file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f'[telegram] send failed: {type(e).__name__}: {e}', file=sys.stderr)
        return False


def _test():
    print(f'Configured: {is_configured()}')
    if not is_configured():
        print('Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env to test.')
        return
    test_msg = (
        '<b>Test notification from trading bot</b>\n'
        'If you see this, Telegram alerts are working.'
    )
    ok = send(test_msg)
    print(f'Send result: {ok}')


if __name__ == '__main__':
    _test()
