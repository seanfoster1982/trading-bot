"""Offline tests for Telegram credential loading and chat-id discovery."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import telegram_notifier as tg  # noqa: E402


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError("nope", "", 0)
        return self._payload


def test_unconfigured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(tg, "load_dotenv", lambda **kwargs: None)
    assert not tg.is_configured()
    assert tg.send("hello") is False


def test_send_html_then_plain_fallback(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(tg, "load_dotenv", lambda **kwargs: None)
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json)
        if json.get("parse_mode") == "HTML":
            return _Resp(400, {"ok": False})
        return _Resp(200, {"ok": True})

    monkeypatch.setattr(tg.httpx, "post", fake_post)
    assert tg.send("<b>hi</b>") is True
    assert len(calls) == 2
    assert calls[0].get("parse_mode") == "HTML"
    assert "parse_mode" not in calls[1]


def test_send_message_alias(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(tg, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setattr(tg.httpx, "post", lambda *a, **k: _Resp(200, {"ok": True}))
    assert tg.send_message("x") is True


def test_discover_chat_id_uses_latest(monkeypatch):
    payload = {
        "ok": True,
        "result": [
            {"message": {"chat": {"id": 111}}},
            {"message": {"chat": {"id": 222}}},
        ],
    }
    monkeypatch.setattr(tg.httpx, "get", lambda *a, **k: _Resp(200, payload))
    assert tg.discover_chat_id("tok") == "222"


def test_discover_chat_id_empty(monkeypatch):
    monkeypatch.setattr(
        tg.httpx, "get", lambda *a, **k: _Resp(200, {"ok": True, "result": []})
    )
    assert tg.discover_chat_id("tok") is None


def test_upsert_env_sets_and_replaces(tmp_path):
    path = tmp_path / ".env"
    path.write_text("EVM_PRIVATE_KEY=keep\nTELEGRAM_CHAT_ID=old\n", encoding="utf-8")
    tg.upsert_env("TELEGRAM_CHAT_ID", "99", path=path)
    tg.upsert_env("TELEGRAM_BOT_TOKEN", "abc", path=path)
    text = path.read_text(encoding="utf-8")
    assert "EVM_PRIVATE_KEY=keep" in text
    assert "TELEGRAM_CHAT_ID=99" in text
    assert "TELEGRAM_CHAT_ID=old" not in text
    assert "TELEGRAM_BOT_TOKEN=abc" in text


def test_setup_sends_when_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(tg, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setattr(
        tg, "bot_identity", lambda token: {"username": "my_bot", "name": "My"}
    )
    monkeypatch.setattr(tg.httpx, "post", lambda *a, **k: _Resp(200, {"ok": True}))
    assert tg.setup(send_test=True) is True


def test_bot_identity(monkeypatch):
    monkeypatch.setattr(
        tg.httpx,
        "get",
        lambda *a, **k: _Resp(
            200, {"ok": True, "result": {"username": "alerts_bot", "first_name": "Alerts"}}
        ),
    )
    ident = tg.bot_identity("tok")
    assert ident == {"username": "alerts_bot", "name": "Alerts"}


def test_creds_strip_quotes(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", '"abc:def"')
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "'99'")
    monkeypatch.setattr(tg, "load_dotenv", lambda **kwargs: None)
    assert tg._creds() == ("abc:def", "99")


def test_setup_no_chat_prints_username(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(tg, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setattr(
        tg, "bot_identity", lambda token: {"username": "alerts_bot", "name": "Alerts"}
    )
    monkeypatch.setattr(tg, "discover_chat_id", lambda token: None)
    assert tg.setup(send_test=True) is False
    out = capsys.readouterr().out
    assert "@alerts_bot" in out
    assert "t.me/alerts_bot" in out


def test_setup_without_token_does_not_call_api(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(tg, "load_dotenv", lambda **kwargs: None)

    def boom(*a, **k):
        raise AssertionError("should not hit Telegram")

    monkeypatch.setattr(tg.httpx, "get", boom)
    monkeypatch.setattr(tg.httpx, "post", boom)
    assert tg.setup(send_test=True) is False
