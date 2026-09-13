from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import decision_record as dr
import rh_live_trader as rlt


@pytest.fixture()
def mem_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(rlt, "DB_PATH", db)
    rlt.init_db()
    # seed legacy-compatible row via old columns only
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO rh_decisions (ts, event_type, asset, chain, reason, extra) "
        "VALUES (1, 'NO_TRADE', '', 'robinhood', 'seed', '')"
    )
    conn.commit()
    conn.close()
    rlt.init_db()  # idempotent migration
    return db


def test_migration_preserves_rows(mem_db):
    conn = sqlite3.connect(mem_db)
    n = conn.execute("SELECT count(*) FROM rh_decisions").fetchone()[0]
    cols = [r[1] for r in conn.execute("PRAGMA table_info(rh_decisions)")]
    conn.close()
    assert n == 1
    assert "decision_id" in cols
    assert "event_type" in cols


def test_init_db_idempotent(mem_db):
    rlt.init_db()
    rlt.init_db()
    conn = sqlite3.connect(mem_db)
    n = conn.execute("SELECT count(*) FROM rh_decisions").fetchone()[0]
    conn.close()
    assert n == 1


def test_persist_no_trade(mem_db):
    conn = sqlite3.connect(mem_db)
    rec = dr.make_decision(decision="NO_TRADE", reason_codes=["no_candidates"])
    rlt.persist_decision(conn, rec)
    row = conn.execute(
        "SELECT event_type, decision FROM rh_decisions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row[0] == "NO_TRADE"
    assert row[1] == "NO_TRADE"


def test_veto_blocks_send(mem_db, monkeypatch):
    conn = sqlite3.connect(mem_db)
    sends = []

    def fake_send(*a, **k):
        sends.append(1)
        return "0xabc"

    monkeypatch.setattr(rlt, "send_quote_tx", fake_send)
    monkeypatch.setattr(rlt, "zerox_quote", lambda *a, **k: {"buyAmount": "1"})
    monkeypatch.setattr(rlt, "maybe_approve", lambda *a, **k: True)
    monkeypatch.setattr(rlt, "roundtrip_ok", lambda *a, **k: (True, 1.0, "ok"))
    monkeypatch.setattr(
        rlt.rh,
        "validate_quote",
        lambda *a, **k: (True, "ok"),
    )
    monkeypatch.setattr(rlt, "is_halted", lambda: True)
    account = MagicMock()
    account.address = "0x" + "11" * 20
    out = rlt.try_buy(
        MagicMock(),
        conn,
        account,
        {"address": "0x" + "22" * 20, "symbol": "T", "source": "dexscreener"},
        eth_px=2500.0,
    )
    assert out is None
    assert sends == []
    ev = conn.execute(
        "SELECT event_type, risk_code FROM rh_decisions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ev[0] in ("BLOCKED", "RISK_VETO")
    assert ev[1] == "emergency_stop"
    conn.close()


def test_invalid_decision_never_sends(monkeypatch):
    sends = []
    monkeypatch.setattr(rlt, "send_quote_tx", lambda *a, **k: sends.append(1) or "0x")
    with pytest.raises(Exception):
        bad = dr.DecisionRecord.model_construct(
            decision="BUY",
            chain_id=1,
            risk_status="ALLOW",
        )
        # force validate
        dr.DecisionRecord.model_validate(bad.model_dump())
    assert sends == []
