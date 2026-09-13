from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from risk_engine import evaluate_rh_buy


def _base(**over):
    kw = dict(
        live_enabled=True,
        halted=False,
        chain_id=4663,
        trade_usd=3.0,
        budget_left=50.0,
        open_count=0,
        max_open=10,
        realized_pnl=0.0,
        max_realized_loss=20.0,
        seconds_since_last_buy=3600.0,
        min_seconds_between_buys=300.0,
        security_blocked=False,
    )
    kw.update(over)
    return evaluate_rh_buy(**kw)


def test_allow():
    v = _base()
    assert v.allow and v.action == "ALLOW"


def test_emergency_stop():
    v = _base(halted=True)
    assert not v.allow and v.code == "emergency_stop"


def test_budget():
    v = _base(budget_left=1.0, trade_usd=3.0)
    assert not v.allow and v.code == "budget_exhausted"


def test_max_open():
    v = _base(open_count=10, max_open=10)
    assert not v.allow and v.code == "max_open"


def test_cooldown():
    v = _base(seconds_since_last_buy=10.0, min_seconds_between_buys=300.0)
    assert not v.allow and v.code == "cooldown"


def test_security():
    v = _base(security_blocked=True, security_reason="honeypot")
    assert not v.allow and v.code == "security_block"


def test_wrong_chain():
    v = _base(chain_id=1)
    assert not v.allow and v.code == "chain_not_allowed"
