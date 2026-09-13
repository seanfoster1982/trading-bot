"""Offline tests for Robinhood Chain quote validation and token filters."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import rh_chain as rh  # noqa: E402


def _quote(*, to, sell, buy, sell_amount, min_buy, taker=None, data="0xabc"):
    q = {
        "liquidityAvailable": True,
        "sellToken": sell,
        "buyToken": buy,
        "sellAmount": str(sell_amount),
        "minBuyAmount": str(min_buy),
        "allowanceTarget": to,
        "transaction": {"to": to, "data": data, "value": "0", "gas": "21000", "gasPrice": "1"},
    }
    if taker:
        q["taker"] = taker
    return q


def test_native_eth_constant():
    assert rh.NATIVE_ETH.lower().startswith("0xeeee")


def test_usdg_is_not_the_squatter():
    assert rh.USDG.lower() != "0x8218d73c00567a01481495ad1e00d5bb5b4".lower()


def test_blocked_majors():
    blocked, reason = rh.is_blocked_token(rh.WETH, "WETH")
    assert blocked and reason == "major_or_native"
    blocked, reason = rh.is_blocked_token(rh.USDG, "USDG")
    assert blocked


def test_blocked_equity_ticker():
    fake = "0x1111111111111111111111111111111111111111"
    blocked, reason = rh.is_blocked_token(fake, "AAPL")
    assert blocked and "equity" in reason


def test_blocked_cme_ticker():
    fake = "0x1111111111111111111111111111111111111111"
    blocked, reason = rh.is_blocked_token(fake, "CME")
    assert blocked


def test_blocked_usdg_imposter():
    squatter = "0x8218d73C00567A01481495Ad6c5143e00D5BB5b4"
    blocked, reason = rh.is_blocked_token(squatter, "USDG")
    assert blocked and "imposter" in reason


def test_blocked_mega_ticker():
    fake = "0x1111111111111111111111111111111111111111"
    blocked, reason = rh.is_blocked_token(fake, "A" * 40)
    assert blocked and reason == "ticker_too_long"


def test_allows_ordinary_meme():
    fake = "0x1111111111111111111111111111111111111111"
    blocked, reason = rh.is_blocked_token(fake, "CHAIN", "Some meme")
    assert not blocked, reason


def test_validate_quote_happy_native():
    taker = rh.RH_EXPECTED_ADDRESS
    q = _quote(to=rh.ALLOWANCE_HOLDER_CANCUN, sell=rh.NATIVE_ETH, buy=rh.USDG,
               sell_amount=10**15, min_buy=1, taker=taker)
    ok, why = rh.validate_quote(
        q, sell_token=rh.NATIVE_ETH, buy_token=rh.USDG,
        taker=taker, sell_amount=10**15)
    assert ok, why


def test_validate_quote_rejects_settler_style_split():
    """transaction.to must equal allowanceTarget (AllowanceHolder)."""
    taker = rh.RH_EXPECTED_ADDRESS
    holder = rh.ALLOWANCE_HOLDER_CANCUN
    settler = "0x1111111111111111111111111111111111111111"
    q = _quote(to=settler, sell=rh.USDG, buy=rh.NATIVE_ETH,
               sell_amount=1, min_buy=1, taker=taker)
    q["allowanceTarget"] = holder
    ok, why = rh.validate_quote(
        q, sell_token=rh.USDG, buy_token=rh.NATIVE_ETH,
        taker=taker, sell_amount=1)
    assert not ok
    assert why == "transaction.to_is_not_allowanceTarget"


def test_validate_quote_rejects_token_mismatch():
    taker = rh.RH_EXPECTED_ADDRESS
    q = _quote(to=rh.ALLOWANCE_HOLDER_CANCUN, sell=rh.NATIVE_ETH, buy=rh.USDG,
               sell_amount=10, min_buy=1, taker=taker)
    ok, why = rh.validate_quote(
        q, sell_token=rh.NATIVE_ETH, buy_token=rh.WETH,
        taker=taker, sell_amount=10)
    assert not ok and why == "buyToken_mismatch"


def test_validate_quote_rejects_permit2():
    taker = rh.RH_EXPECTED_ADDRESS
    q = _quote(to=rh.PERMIT2, sell=rh.USDG, buy=rh.NATIVE_ETH,
               sell_amount=1, min_buy=1, taker=taker)
    ok, why = rh.validate_quote(
        q, sell_token=rh.USDG, buy_token=rh.NATIVE_ETH,
        taker=taker, sell_amount=1)
    assert not ok
    assert why == "permit2_not_supported"


def test_approve_spender_must_match_target():
    ok, why = rh.validate_approve_spender(rh.PERMIT2, rh.PERMIT2)
    assert not ok
    ok, why = rh.validate_approve_spender(
        rh.ALLOWANCE_HOLDER_CANCUN, rh.ALLOWANCE_HOLDER_CANCUN)
    assert ok, why
    ok, why = rh.validate_approve_spender(
        "0x1111111111111111111111111111111111111111",
        rh.ALLOWANCE_HOLDER_CANCUN)
    assert not ok


def test_roundtrip_eth_pct():
    assert rh.roundtrip_eth_pct(100, 97) == pytest.approx(3.0)
    assert rh.roundtrip_eth_pct(100, 100) == 0.0
    assert rh.roundtrip_eth_pct(0, 1) == 100.0


def test_assemble_legacy_tx_chain_id():
    tx = rh.assemble_legacy_tx(
        to=rh.ALLOWANCE_HOLDER_CANCUN, data="0xabcd", value=1,
        gas=21000, gas_price=1, nonce=0)
    assert tx["chainId"] == 4663
    assert tx["value"] == 1
    assert tx["data"] == bytes.fromhex("abcd")


def test_classify_fresh_listing():
    assert rh.classify_listing(
        age_min=15, liquidity=3000, volume_1h=500,
        fresh_max_age_min=120, fresh_min_liq=2000, fresh_min_vol=400,
        min_liq=15000, min_vol=2000,
    ) == "fresh"


def test_classify_fresh_too_thin():
    assert rh.classify_listing(
        age_min=15, liquidity=100, volume_1h=10,
        fresh_max_age_min=120, fresh_min_liq=2000, fresh_min_vol=400,
        min_liq=15000, min_vol=2000,
    ) is None


def test_classify_established():
    assert rh.classify_listing(
        age_min=400, liquidity=20000, volume_1h=3000,
        fresh_max_age_min=120, fresh_min_liq=2000, fresh_min_vol=400,
        min_liq=15000, min_vol=2000,
    ) == "established"
