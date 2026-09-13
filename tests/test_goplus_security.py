"""GoPlus Token Security (RH 4663) — adapter + BUY-only gate. No live broadcasts."""
from __future__ import annotations

import inspect
import json
import logging
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import decision_record as dr  # noqa: E402
import goplus_security as gp  # noqa: E402
import rh_chain as rh  # noqa: E402
import rh_live_trader as rlt  # noqa: E402
import whale_config as wc  # noqa: E402

TOKEN_A = "0x" + "11" * 20
TOKEN_B = "0x" + "22" * 20
DOC_KEY = "mBOMg20QW11BbtyH4Zh0"
DOC_SECRET = "V6aRfxlPJwN3ViJSIFSCdxPvneajuJsh"
DOC_TIME = 1647847498
DOC_SIGN = "7293d385b9225b3c3f232b76ba97255d0e21063e"
ACCESS = "test-access-token-NOT-REAL"
SECRET = "super-secret-app-secret-xyz"


class FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError("nope", "", 0)
        return self._payload


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json, headers))
        return self.handler("POST", url, json, headers)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, headers))
        return self.handler("GET", url, params, headers)

    def close(self):
        return None


def _timeout():
    req = httpx.Request("GET", gp.GOPLUS_BASE + "/api/v1/token_security/4663")
    return httpx.TimeoutException("timed out", request=req)


def _clean_payload(**over):
    body = {
        "is_honeypot": "0",
        "owner_change_balance": "0",
        "cannot_buy": "0",
        "is_open_source": "1",
        "is_proxy": "0",
        "token_symbol": "SAFE",
    }
    body.update(over)
    return body


def _security_ok_handler(address=TOKEN_A, payload=None):
    payload = payload or _clean_payload()

    def handler(method, url, json_body, headers):
        if method == "POST":
            return FakeResp(
                200,
                {"code": 1, "result": {"access_token": ACCESS, "expires_in": 7200}},
            )
        assert "token_security/4663" in url
        if method == "GET":
            params = json_body  # FakeClient passes params in this slot
            addr = (params or {}).get("contract_addresses")
            assert addr == rh.checksum(address).lower()
            assert headers["Authorization"] == ACCESS  # GoPlus: raw token, not Bearer
            return FakeResp(
                200, {"code": 1, "result": {addr: payload}}
            )
        return FakeResp(500, {"code": 0})

    return handler


def _enabled_cfg():
    return gp.GoPlusConfig(enabled=True, app_key="app-key", app_secret=SECRET)


def _checker(handler, now=1_700_000_000.0):
    clock = {"t": now}

    def now_fn():
        return clock["t"]

    client = FakeClient(handler)
    checker = gp.GoPlusChecker(
        config=_enabled_cfg(), client=client, now_fn=now_fn
    )
    checker._clock = clock
    return checker, client, clock


@pytest.fixture()
def isolated_env(monkeypatch):
    monkeypatch.setenv("GOPLUS_ENABLED", "false")
    monkeypatch.setenv("GOPLUS_APP_KEY", "")
    monkeypatch.setenv("GOPLUS_APP_SECRET", "")
    gp.reset_checker()
    yield
    gp.reset_checker()


@pytest.fixture()
def mem_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(rlt, "DB_PATH", db)
    rlt.init_db()
    return db


def _stub_executable_buy(monkeypatch):
    monkeypatch.setattr(rlt, "is_halted", lambda: False)
    monkeypatch.setattr(rlt, "roundtrip_ok", lambda *a, **k: (True, 1.0, "ok"))
    monkeypatch.setattr(rlt, "zerox_quote", lambda *a, **k: {"buyAmount": "1"})
    monkeypatch.setattr(rlt.rh, "validate_quote", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(rlt, "maybe_approve", lambda *a, **k: True)
    monkeypatch.setattr(
        rlt, "wait_receipt", lambda *a, **k: {"status": "0x1"}
    )
    monkeypatch.setattr(rlt, "erc20_decimals", lambda *a, **k: 18)
    monkeypatch.setattr(rlt, "erc20_balance", lambda *a, **k: 10**18)
    monkeypatch.setattr(rlt.telegram_notifier, "send", lambda *a, **k: True)
    sends: list = []

    def fake_send(*a, **k):
        sends.append({"args": a, "kwargs": k})
        return "0xdead"

    monkeypatch.setattr(rlt, "send_quote_tx", fake_send)
    account = MagicMock()
    account.address = "0x" + "ab" * 20
    return account, sends


def _run_buy(conn, account, address=TOKEN_A):
    return rlt.try_buy(
        MagicMock(),
        conn,
        account,
        {"address": address, "symbol": "T", "source": "dexscreener"},
        eth_px=2500.0,
    )


# --- adapter -----------------------------------------------------------------


def test_documented_access_token_signature():
    assert gp.sign_access_token_request(DOC_KEY, DOC_TIME, DOC_SECRET) == DOC_SIGN


def test_disabled_makes_zero_http_calls(isolated_env):
    def boom(*a, **k):
        raise AssertionError("GoPlus HTTP must not run when disabled")

    checker = gp.GoPlusChecker(
        config=gp.GoPlusConfig(enabled=False, app_key="k", app_secret=SECRET),
        client=FakeClient(boom),
    )
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.status == gp.STATUS_DISABLED
    assert out.reason_codes == ["goplus_disabled"]
    assert gp.blocks_new_buy(out) is False


def test_module_disabled_zero_calls(isolated_env, monkeypatch):
    monkeypatch.setenv("GOPLUS_ENABLED", "false")
    gp.reset_checker()
    calls = []

    class BoomClient:
        def __init__(self, *a, **k):
            raise AssertionError("httpx.Client must not be constructed")

    monkeypatch.setattr(gp.httpx, "Client", BoomClient)
    out = gp.check_token_security(4663, TOKEN_A)
    assert out.status == gp.STATUS_DISABLED
    assert calls == []


def test_access_token_flow_mocked():
    checker, client, _ = _checker(_security_ok_handler())
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.status == gp.STATUS_PASS
    assert out.hard_block is False
    assert "goplus_pass" in out.reason_codes
    posts = [c for c in client.calls if c[0] == "POST"]
    gets = [c for c in client.calls if c[0] == "GET"]
    assert len(posts) == 1
    body = posts[0][2]
    assert body["app_key"] == "app-key"
    assert "time" in body
    assert body["sign"] == gp.sign_access_token_request(
        "app-key", body["time"], SECRET
    )
    assert len(gets) == 1
    assert "token_security/4663" in gets[0][1]


def test_secret_and_token_never_in_logs(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    checker, client, _ = _checker(_security_ok_handler())
    out = checker.check_token_security(4663, TOKEN_A)
    text = caplog.text + capsys.readouterr().out + capsys.readouterr().err
    text += json.dumps(out.to_public_dict())
    text += json.dumps(out.to_extra())
    assert SECRET not in text
    assert ACCESS not in text
    assert DOC_SECRET not in text


def test_pass_is_not_hard_block():
    checker, _, _ = _checker(_security_ok_handler())
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.status == gp.STATUS_PASS
    assert out.hard_block is False
    assert gp.blocks_new_buy(out) is False


def test_honeypot_hard_block():
    h = _security_ok_handler(payload=_clean_payload(is_honeypot="1"))
    checker, _, _ = _checker(h)
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.hard_block is True
    assert out.status == gp.STATUS_BLOCK
    assert "goplus_honeypot" in out.reason_codes
    assert "is_honeypot" in out.hard_block_reasons


def test_cannot_sell_hard_block():
    h = _security_ok_handler(payload=_clean_payload(cannot_sell="1"))
    checker, _, _ = _checker(h)
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.hard_block is True
    assert "goplus_cannot_sell" in out.reason_codes


def test_owner_change_balance_hard_block():
    h = _security_ok_handler(payload=_clean_payload(owner_change_balance="1"))
    checker, _, _ = _checker(h)
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.hard_block is True
    assert "goplus_balance_control" in out.reason_codes


def test_proxy_is_warning_not_hard_block():
    h = _security_ok_handler(payload=_clean_payload(is_proxy="1"))
    checker, _, _ = _checker(h)
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.hard_block is False
    assert out.status == gp.STATUS_WARN
    assert "goplus_proxy" in out.reason_codes
    assert gp.blocks_new_buy(out) is False


def test_not_open_source_is_warning_when_honeypot_known():
    h = _security_ok_handler(
        payload=_clean_payload(is_open_source="0")
    )
    checker, _, _ = _checker(h)
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.hard_block is False
    assert out.status == gp.STATUS_WARN


def test_timeout_unavailable():
    def handler(method, url, json_body, headers):
        if method == "POST":
            return FakeResp(
                200,
                {"code": 1, "result": {"access_token": ACCESS, "expires_in": 7200}},
            )
        raise _timeout()

    checker, _, _ = _checker(handler)
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.status == gp.STATUS_UNAVAILABLE
    assert out.error_code == "goplus_unavailable"
    assert gp.blocks_new_buy(out) is True


def test_http_401_auth_failed():
    def handler(method, url, json_body, headers):
        if method == "POST":
            return FakeResp(
                200,
                {"code": 1, "result": {"access_token": ACCESS, "expires_in": 7200}},
            )
        return FakeResp(401, {"code": 0})

    checker, _, _ = _checker(handler)
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.error_code == "goplus_auth_failed"
    assert gp.blocks_new_buy(out) is True


def test_http_403_auth_failed():
    def handler(method, url, json_body, headers):
        if method == "POST":
            return FakeResp(
                200,
                {"code": 1, "result": {"access_token": ACCESS, "expires_in": 7200}},
            )
        return FakeResp(403, {"code": 0})

    checker, _, _ = _checker(handler)
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.error_code == "goplus_auth_failed"


def test_http_429_rate_limited():
    def handler(method, url, json_body, headers):
        if method == "POST":
            return FakeResp(
                200,
                {"code": 1, "result": {"access_token": ACCESS, "expires_in": 7200}},
            )
        return FakeResp(429, {"code": 0})

    checker, _, _ = _checker(handler)
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.error_code == "goplus_rate_limited"


def test_http_5xx_unavailable():
    def handler(method, url, json_body, headers):
        if method == "POST":
            return FakeResp(
                200,
                {"code": 1, "result": {"access_token": ACCESS, "expires_in": 7200}},
            )
        return FakeResp(503, {"code": 0})

    checker, _, _ = _checker(handler)
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.error_code == "goplus_unavailable"


def test_malformed_json_unavailable():
    def handler(method, url, json_body, headers):
        if method == "POST":
            return FakeResp(
                200,
                {"code": 1, "result": {"access_token": ACCESS, "expires_in": 7200}},
            )
        return FakeResp(200, None, text="<html>")

    checker, _, _ = _checker(handler)
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.error_code == "goplus_malformed"


def test_partial_empty_result_not_pass():
    def handler(method, url, json_body, headers):
        if method == "POST":
            return FakeResp(
                200,
                {"code": 1, "result": {"access_token": ACCESS, "expires_in": 7200}},
            )
        params = json_body
        return FakeResp(200, {"code": 1, "result": {}})

    checker, _, _ = _checker(handler)
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.status == gp.STATUS_UNAVAILABLE
    assert out.error_code == "goplus_partial"
    assert out.hard_block is False
    assert gp.blocks_new_buy(out) is True


def test_partial_missing_honeypot_not_pass():
    h = _security_ok_handler(payload={"owner_change_balance": "0", "token_symbol": "X"})
    checker, _, _ = _checker(h)
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.error_code == "goplus_partial"
    assert gp.blocks_new_buy(out) is True


def test_cache_prevents_duplicate_security_get():
    checker, client, _ = _checker(_security_ok_handler())
    a = checker.check_token_security(4663, TOKEN_A)
    b = checker.check_token_security(4663, TOKEN_A)
    assert a.status == gp.STATUS_PASS
    assert b.data_freshness["from_cache"] is True
    assert len([c for c in client.calls if c[0] == "GET"]) == 1
    assert len([c for c in client.calls if c[0] == "POST"]) == 1


def test_expired_cache_refreshes():
    checker, client, clock = _checker(_security_ok_handler())
    checker.check_token_security(4663, TOKEN_A)
    clock["t"] += gp.CACHE_TTL_S + 1
    checker.check_token_security(4663, TOKEN_A)
    assert len([c for c in client.calls if c[0] == "GET"]) == 2


def test_cache_key_includes_chain_and_contract():
    def handler(method, url, json_body, headers):
        if method == "POST":
            return FakeResp(
                200,
                {"code": 1, "result": {"access_token": ACCESS, "expires_in": 7200}},
            )
        # allow both chain ids
        params = json_body
        addr = (params or {}).get("contract_addresses")
        return FakeResp(
            200, {"code": 1, "result": {addr: _clean_payload()}}
        )

    checker, client, _ = _checker(handler)
    checker.check_token_security(4663, TOKEN_A)
    checker.check_token_security(1, TOKEN_A)
    checker.check_token_security(4663, TOKEN_B)
    gets = [c for c in client.calls if c[0] == "GET"]
    assert len(gets) == 3
    assert sum(1 for c in gets if "token_security/4663" in c[1]) == 2
    assert sum(1 for c in gets if "token_security/1" in c[1]) == 1


def test_rh_candidates_use_chain_4663():
    checker, client, _ = _checker(_security_ok_handler())
    checker.check_token_security(rh.RH_CHAIN_ID, TOKEN_A)
    gets = [c for c in client.calls if c[0] == "GET"]
    assert gets and "/token_security/4663" in gets[0][1]


def test_invalid_contract_address_safe():
    checker, client, _ = _checker(_security_ok_handler())
    out = checker.check_token_security(4663, "not-an-address")
    assert out.error_code == "goplus_invalid_address"
    assert gp.blocks_new_buy(out) is True
    assert client.calls == []


def test_checker_never_raises_on_client_explosion():
    def handler(*a, **k):
        raise RuntimeError("boom")

    checker, _, _ = _checker(handler)
    out = checker.check_token_security(4663, TOKEN_A)
    assert out.status == gp.STATUS_UNAVAILABLE
    assert SECRET not in json.dumps(out.to_public_dict())


def test_config_repr_hides_secret():
    cfg = gp.GoPlusConfig(enabled=True, app_key="k", app_secret=SECRET)
    assert SECRET not in repr(cfg)


# --- live BUY/SELL integration (mocked, never broadcasts) ------------------


def test_hard_block_creates_blocked_decision_and_zero_sends(mem_db, monkeypatch):
    account, sends = _stub_executable_buy(monkeypatch)
    blocked = gp._result(
        chain_id=4663,
        contract_address=TOKEN_A,
        now=1,
        status=gp.STATUS_BLOCK,
        hard_block=True,
        hard_block_reasons=["is_honeypot"],
        reason_codes=["goplus_honeypot"],
    )
    monkeypatch.setattr(rlt.goplus, "check_token_security", lambda *a, **k: blocked)
    conn = sqlite3.connect(mem_db)
    out = _run_buy(conn, account)
    assert out is None
    assert sends == []
    row = conn.execute(
        "SELECT decision, security_status, risk_code, reason_codes_json, sources_json "
        "FROM rh_decisions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "BLOCKED"
    assert row[1] == "BLOCK"
    assert "goplus_honeypot" in row[2] or "goplus_honeypot" in row[3]
    assert "goplus" in row[4]
    conn.close()


@pytest.mark.parametrize(
    "err",
    [
        "goplus_unavailable",
        "goplus_auth_failed",
        "goplus_rate_limited",
        "goplus_malformed",
        "goplus_partial",
    ],
)
def test_goplus_failure_blocks_new_buy(mem_db, monkeypatch, err):
    account, sends = _stub_executable_buy(monkeypatch)
    bad = gp._result(
        chain_id=4663,
        contract_address=TOKEN_A,
        now=1,
        status=gp.STATUS_UNAVAILABLE,
        error_code=err,
        reason_codes=[err],
        raw_status_code=503 if err == "goplus_unavailable" else 401,
    )
    monkeypatch.setattr(rlt.goplus, "check_token_security", lambda *a, **k: bad)
    conn = sqlite3.connect(mem_db)
    out = _run_buy(conn, account)
    assert out is None
    assert sends == []
    row = conn.execute(
        "SELECT decision, security_status FROM rh_decisions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "BLOCKED"
    assert row[1] == "UNKNOWN"
    conn.close()


def test_pass_does_not_security_veto(mem_db, monkeypatch):
    account, sends = _stub_executable_buy(monkeypatch)
    ok = gp._result(
        chain_id=4663,
        contract_address=TOKEN_A,
        now=1,
        status=gp.STATUS_PASS,
        reason_codes=["goplus_pass"],
    )
    monkeypatch.setattr(rlt.goplus, "check_token_security", lambda *a, **k: ok)
    conn = sqlite3.connect(mem_db)
    _run_buy(conn, account)
    assert sends, "PASS must not veto an otherwise executable BUY"
    row = conn.execute(
        "SELECT decision, security_status, reason_codes_json FROM rh_decisions "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "BUY"
    assert row[1] == "PASS"
    assert "goplus_pass" in row[2]
    conn.close()


def test_disabled_buy_records_disabled_and_may_send(mem_db, monkeypatch, isolated_env):
    account, sends = _stub_executable_buy(monkeypatch)
    conn = sqlite3.connect(mem_db)
    _run_buy(conn, account)
    assert sends, "disabled GoPlus must preserve existing BUY path"
    extra = conn.execute(
        "SELECT extra_json FROM rh_decisions WHERE decision='BUY' ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert "DISABLED" in extra
    conn.close()


def test_goplus_outage_does_not_block_sell(mem_db, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("GoPlus must not gate SELL/exit")

    monkeypatch.setattr(rlt.goplus, "check_token_security", boom)
    monkeypatch.setattr(rlt, "mark_price_usd", lambda *a, **k: 1.0)
    monkeypatch.setattr(rlt, "erc20_decimals", lambda *a, **k: 18)
    monkeypatch.setattr(rlt, "erc20_balance", lambda *a, **k: 10**18)
    monkeypatch.setattr(rlt, "zerox_quote", lambda *a, **k: {"buyAmount": str(10**15)})
    monkeypatch.setattr(rlt.rh, "validate_quote", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(rlt, "maybe_approve", lambda *a, **k: True)
    monkeypatch.setattr(rlt, "wait_receipt", lambda *a, **k: {"status": "0x1"})
    monkeypatch.setattr(rlt.telegram_notifier, "send", lambda *a, **k: True)
    sends: list = []
    monkeypatch.setattr(
        rlt, "send_quote_tx", lambda *a, **k: sends.append("sell") or "0xsell"
    )
    conn = sqlite3.connect(mem_db)
    conn.execute(
        """
        INSERT INTO rh_live_trades (
            chain_id, address, symbol, source, opened_at, entry_price,
            usd_spent, eth_spent, tokens, buy_tx
        ) VALUES (?, ?, 'T', 'dexscreener', 1, 10.0, 3.0, 0.001, 1.0, '0xbuy')
        """,
        (rh.RH_CHAIN_ID, rh.checksum(TOKEN_A)),
    )
    conn.commit()
    account = MagicMock()
    account.address = "0x" + "ab" * 20
    rlt.manage_open(MagicMock(), conn, account, eth_px=2500.0)
    assert sends == ["sell"]
    conn.close()


def test_status_is_non_trading(mem_db, monkeypatch):
    monkeypatch.setattr(rlt, "load_account", lambda: None)
    monkeypatch.setattr(rlt, "eth_balance", lambda *a, **k: 0.0)
    monkeypatch.setattr(rlt, "eth_price_usd", lambda *a, **k: 0.0)
    monkeypatch.setattr(
        rlt.goplus,
        "check_token_security",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("status must not query GoPlus")),
    )
    sends = []
    monkeypatch.setattr(rlt, "send_quote_tx", lambda *a, **k: sends.append(1))
    rlt.status()
    assert sends == []
    assert "check_token_security" not in inspect.getsource(rlt.status)
    assert "send_quote_tx" not in inspect.getsource(rlt.status)


def test_manage_open_does_not_call_goplus():
    assert "check_token_security" not in inspect.getsource(rlt.manage_open)


def test_caps_and_flags_unchanged():
    assert wc.RH_TRADE_USD == 3.0
    assert wc.RH_BUDGET_USD == 80.0
    assert wc.RH_MAX_OPEN == 10
    assert wc.RH_LIVE_ENABLED is True
    assert wc.LIVE_ENABLED is False


def test_env_gitignored():
    gi = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gi.splitlines()[0] or ".env" in gi


def test_env_example_names_only():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "GOPLUS_ENABLED=false" in text
    assert "GOPLUS_APP_KEY=" in text
    assert "GOPLUS_APP_SECRET=" in text
    for line in text.splitlines():
        if line.startswith("GOPLUS_APP_KEY=") or line.startswith("GOPLUS_APP_SECRET="):
            assert line.split("=", 1)[1] == ""


def test_no_runtime_health_file_on_canonical_sha():
    # Phase 1B on Windows/canonical did not add test_rh_runtime_health.py
    assert not (REPO_ROOT / "tests" / "test_rh_runtime_health.py").exists()


def test_smoke_cli_help_is_non_trading():
    src = inspect.getsource(gp)
    assert "sign_transaction" not in src
    assert "sendRawTransaction" not in src
    assert "EVM_PRIVATE_KEY" not in src


def test_smoke_test_missing_creds_nonzero(isolated_env, monkeypatch):
    monkeypatch.setattr(gp, "read_config", lambda: gp.GoPlusConfig(False, "", ""))
    assert gp.smoke_test() != 0


def test_authorization_header_is_raw_access_token(monkeypatch):
    """GoPlus rejects Authorization: Bearer <token> with code 4012; send raw token."""
    import goplus_security as gp

    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "code": 1,
                "message": "ok",
                "result": {
                    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": {
                        "is_honeypot": "0",
                        "is_open_source": "1",
                        "is_proxy": "0",
                        "is_mintable": "0",
                        "owner_change_balance": "0",
                        "can_take_back_ownership": "0",
                        "hidden_owner": "0",
                        "selfdestruct": "0",
                        "external_call": "0",
                        "buy_tax": "0",
                        "sell_tax": "0",
                        "cannot_buy": "0",
                        "cannot_sell_all": "0",
                        "slippage_modifiable": "0",
                        "is_blacklisted": "0",
                        "is_whitelisted": "0",
                        "is_anti_whale": "0",
                        "anti_whale_modifiable": "0",
                        "trading_cooldown": "0",
                        "personal_slippage_modifiable": "0",
                    }
                },
            }

    class FakeClient:
        def get(self, url, params=None, headers=None, timeout=None):
            captured["headers"] = dict(headers or {})
            captured["url"] = url
            return FakeResp()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    checker = gp.GoPlusChecker(
        config=gp.GoPlusConfig(enabled=True, app_key="k", app_secret="s")
    )
    checker._access_token = "test-access-token-value"
    checker._access_expires_at = 10**12
    monkeypatch.setattr(gp.httpx, "Client", lambda *a, **k: FakeClient())
    # Direct fetch path
    result = checker._fetch_token_security(
        FakeClient(),
        "test-access-token-value",
        1,
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        0.0,
    )
    assert captured["headers"].get("Authorization") == "test-access-token-value"
    assert not str(captured["headers"].get("Authorization", "")).startswith("Bearer ")
    assert result.status in (gp.STATUS_PASS, gp.STATUS_WARN, gp.STATUS_BLOCK)


def test_smoke_ok_on_partial_normalized_weth(monkeypatch, capsys):
    """Smoke passes when auth/API/normalize work even if is_honeypot is absent."""
    import goplus_security as gp

    partial = gp._result(
        chain_id=4663,
        contract_address=gp.SMOKE_WETH,
        now=0.0,
        status=gp.STATUS_UNAVAILABLE,
        raw_status_code=200,
        error_code="goplus_partial",
        reason_codes=["goplus_partial"],
        warnings=["is_proxy"],
        payload={"token_symbol": "WETH", "is_proxy": "1"},
    )

    class FakeChecker:
        def __init__(self, *a, **k):
            pass

        def check_token_security(self, chain_id, address):
            return partial

        @property
        def _access_token(self):
            return None

    monkeypatch.setattr(gp, "GoPlusChecker", FakeChecker)
    monkeypatch.setattr(
        gp,
        "read_config",
        lambda: gp.GoPlusConfig(enabled=False, app_key="k", app_secret="s"),
    )
    rc = gp.smoke_test(gp.SMOKE_WETH)
    assert rc == 0
    out = capsys.readouterr().out
    assert "smoke-test OK" in out

