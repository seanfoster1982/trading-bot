from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import pytest
from pydantic import ValidationError

from decision_record import Decision, DecisionRecord, RH_CHAIN_ID, make_decision


def test_invalid_decision_fails():
    with pytest.raises(ValidationError):
        make_decision(decision="MAYBE")


def test_negative_notional_fails():
    with pytest.raises(ValidationError):
        make_decision(decision="NO_TRADE", proposed_notional_usd=-1)


def test_confidence_out_of_range_fails():
    with pytest.raises(ValidationError):
        make_decision(decision="HOLD", confidence=1.5)


def test_wrong_chain_buy_fails():
    with pytest.raises(ValidationError):
        make_decision(
            decision="BUY",
            chain_id=1,
            risk_status="ALLOW",
            proposed_notional_usd=3.0,
        )


def test_buy_requires_allow():
    with pytest.raises(ValidationError):
        make_decision(
            decision="BUY",
            chain_id=RH_CHAIN_ID,
            risk_status="VETO",
            proposed_notional_usd=3.0,
        )


def test_valid_buy():
    rec = make_decision(
        decision=Decision.BUY,
        chain_id=RH_CHAIN_ID,
        risk_status="ALLOW",
        risk_code="ok",
        proposed_notional_usd=3.0,
        reason_codes=["quote_ok"],
        sources=["dexscreener"],
    )
    assert rec.is_executable()
    assert rec.decision == Decision.BUY


def test_no_trade_ok():
    rec = make_decision(decision="NO_TRADE", reason_codes=["no_candidates"])
    assert not rec.is_executable()
