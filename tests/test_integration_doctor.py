"""Tests for integration doctor / adapters — mocked, no live trades."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_defillama_smoke_ok(monkeypatch):
    import defillama_client as d

    class R:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"name": "Demo", "tvl": 1}]

    class C:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return R()

        def close(self):
            return None

    monkeypatch.setattr(d.httpx, "Client", lambda **k: C())
    assert d.smoke_test() == 0


def test_defillama_timeout(monkeypatch):
    import defillama_client as d
    import httpx

    def boom(**k):
        raise httpx.TimeoutException("t")

    monkeypatch.setattr(d.httpx, "Client", boom)
    assert d.smoke_test() == 1


def test_etherscan_missing_key(monkeypatch):
    import etherscan_client as e

    monkeypatch.setenv("ETHERSCAN_API_KEY", "")
    assert e.smoke_test() == 2


def test_etherscan_401(monkeypatch):
    import etherscan_client as e

    monkeypatch.setenv("ETHERSCAN_API_KEY", "dummy")

    class R:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "0", "message": "NOTOK", "result": "Invalid API Key"}

    class C:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            assert "apikey" in k.get("params", {})
            return R()

    monkeypatch.setattr(e.httpx, "Client", lambda **k: C())
    assert e.smoke_test() == 1


def test_chainabuse_no_meter_without_confirm(monkeypatch, tmp_path):
    import chainabuse_client as c

    monkeypatch.setenv("CHAINABUSE_API_KEY", "dummy")
    monkeypatch.setattr(c, "COUNTER", tmp_path / "cap.json")
    assert c.smoke_test(confirm_metered=False) == 0


def test_chainabuse_cap(monkeypatch, tmp_path):
    import chainabuse_client as c
    import json

    monkeypatch.setenv("CHAINABUSE_API_KEY", "dummy")
    monkeypatch.setenv("CHAINABUSE_MONTHLY_CALL_CAP", "1")
    p = tmp_path / "cap.json"
    p.write_text(json.dumps({"ym": __import__("time").strftime("%Y-%m"), "count": 1}), encoding="utf-8")
    monkeypatch.setattr(c, "COUNTER", p)
    assert c.smoke_test(confirm_metered=True) == 1


def test_x_disabled_default(monkeypatch):
    import x_social as x

    monkeypatch.setenv("X_BEARER_TOKEN", "")
    monkeypatch.setenv("X_ENABLED", "false")
    assert x.smoke_test() == 2
    assert x.enabled() is False


def test_discord_gateway_refuses():
    import discord_gateway as g

    with pytest.raises(RuntimeError):
        g.start_gateway()


def test_doctor_hides_secrets(monkeypatch, capsys):
    import integration_doctor as doc

    monkeypatch.setenv("GOPLUS_APP_SECRET", "supersecretvalue")
    monkeypatch.setenv("BIRDEYE_API_KEY", "birdsecret")
    # force fast paths
    monkeypatch.setattr(doc, "dexscreener", lambda: "PASS")
    monkeypatch.setattr(doc, "_run_smoke", lambda *a, **k: "PASS")
    monkeypatch.setattr(doc, "_present", lambda name: True)
    doc.main()
    out = capsys.readouterr().out
    assert "supersecretvalue" not in out
    assert "birdsecret" not in out


def test_finbert_missing_deps(monkeypatch):
    import finbert_sentiment as f
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("transformers") or name == "torch":
            raise ImportError("missing")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # analyze should raise RuntimeError; smoke returns 2
    # Reset module analyze path by calling smoke which imports inside analyze
    # Directly patch analyze
    def boom(texts):
        raise RuntimeError("FinBERT deps missing")

    monkeypatch.setattr(f, "analyze", boom)
    assert f.smoke_test() == 2
