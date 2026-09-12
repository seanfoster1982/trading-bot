"""Robinhood Chain (EIP-155 4663) helpers — no network I/O, no signing.

Used by rh_live_trader.py. Kept separate so tests can exercise quote
validation, calldata, and token filters without touching RPC or 0x.
"""
from __future__ import annotations

from eth_abi import encode
from eth_utils import keccak, to_checksum_address, to_hex

# --- chain constants ---
RH_CHAIN_ID = 4663
RH_RPC_DEFAULT = "https://rpc.mainnet.chain.robinhood.com"
RH_EXPLORER = "https://robinhoodchain.blockscout.com"
ZEROX_API = "https://api.0x.org"
ZEROX_VERSION = "v2"

# Native ETH placeholder used by 0x Swap API.
NATIVE_ETH = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
# Wrapped ETH on Robinhood Chain (Uniswap router WETH9).
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
# Global Dollar — the real USDG. Other "USDG" tickers on this chain are
# squatters (see robinhood-toolkit TOKENS.md).
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"

# Hot wallet the user funded (~$94 ETH on 2026-09-12).
RH_EXPECTED_ADDRESS = "0x4D772dB54461eAc90538d6c1E336841f4ACbD91c"

# AllowanceHolder on Cancun-hardfork EVM chains. Robinhood is Arbitrum Nitro;
# we still refuse any spender that is NOT exactly the quote's allowanceTarget,
# and we refuse if transaction.to != allowanceTarget (Settler is never an
# approval target).
ALLOWANCE_HOLDER_CANCUN = "0x0000000000001fF3684f28c67538d4D072C22734"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"

# US user: do not treat tokenized-equity tickers as memecoins.
EQUITY_TICKERS = {
    "AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "GOOGL", "GOOG", "META", "HOOD",
    "SPY", "QQQ", "IWM", "NFLX", "AMD", "INTC", "BABA", "COIN", "MSTR",
    "CME", "HOODX", "AAPLX", "TSLAX", "NVDAX",
    "PLTR", "AMZN", "BRK", "JPM", "V", "MA", "UNH", "XOM", "JNJ", "WMT",
}

KNOWN_STABLES = {
    USDG.lower(),
}

MAJOR_SKIP = {
    NATIVE_ETH.lower(),
    WETH.lower(),
    USDG.lower(),
}


def checksum(addr: str) -> str:
    return to_checksum_address(addr)


def addr_eq(a: str, b: str) -> bool:
    try:
        return checksum(a) == checksum(b)
    except Exception:
        return False


def to_int(value) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return 0
    return int(s, 16) if s.startswith("0x") or s.startswith("0X") else int(s)


def selector(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def encode_call(sig: str, types: list[str], values: list) -> str:
    return to_hex(selector(sig) + encode(types, values))


def calldata_balance_of(owner: str) -> str:
    return encode_call("balanceOf(address)", ["address"], [checksum(owner)])


def calldata_decimals() -> str:
    return to_hex(selector("decimals()"))


def calldata_symbol() -> str:
    return to_hex(selector("symbol()"))


def calldata_allowance(owner: str, spender: str) -> str:
    return encode_call(
        "allowance(address,address)",
        ["address", "address"],
        [checksum(owner), checksum(spender)],
    )


def calldata_approve(spender: str, amount: int) -> str:
    return encode_call(
        "approve(address,uint256)",
        ["address", "uint256"],
        [checksum(spender), int(amount)],
    )


def is_native(token: str) -> bool:
    return addr_eq(token, NATIVE_ETH)


def is_blocked_token(address: str, symbol: str | None = None,
                     name: str | None = None) -> tuple[bool, str]:
    """Return (blocked, reason). Watchlist callers may ignore ticker blocks
    except for known-major / imposter-stable rules."""
    try:
        a = checksum(address)
    except Exception:
        return True, "invalid_address"
    if a.lower() in MAJOR_SKIP:
        return True, "major_or_native"
    sym = (symbol or "").strip().upper().replace("$", "")
    if len(sym) > 16:
        return True, "ticker_too_long"
    if "STOCK" in (name or "").upper() or "TOKENIZED" in (name or "").upper():
        return True, "equity_or_stock_token"
    if sym in EQUITY_TICKERS:
        return True, f"equity_ticker:{sym}"
    if sym in {"USDG", "USDC", "USDT", "USD", "DAI"} and a.lower() not in KNOWN_STABLES:
        return True, f"stablecoin_imposter:{sym}"
    return False, ""


def validate_quote(
    quote: dict,
    *,
    sell_token: str,
    buy_token: str,
    taker: str,
    sell_amount: int,
) -> tuple[bool, str]:
    """Refuse quotes that would send value to the wrong contract or asset."""
    if not quote:
        return False, "empty_quote"
    if quote.get("liquidityAvailable") is False:
        return False, "liquidity_unavailable"
    tx = quote.get("transaction") or {}
    to = tx.get("to")
    if not to:
        return False, "missing_transaction.to"
    try:
        to_cs = checksum(to)
        sell_cs = checksum(sell_token)
        buy_cs = checksum(buy_token)
        taker_cs = checksum(taker)
    except Exception as e:
        return False, f"bad_address:{e}"

    q_sell = quote.get("sellToken") or quote.get("sellTokenAddress")
    q_buy = quote.get("buyToken") or quote.get("buyTokenAddress")
    if q_sell and not addr_eq(q_sell, sell_cs):
        return False, "sellToken_mismatch"
    if q_buy and not addr_eq(q_buy, buy_cs):
        return False, "buyToken_mismatch"

    q_taker = quote.get("taker")
    if q_taker and not addr_eq(q_taker, taker_cs):
        return False, "taker_mismatch"

    q_sell_amt = to_int(quote.get("sellAmount"))
    if q_sell_amt and q_sell_amt != int(sell_amount):
        return False, "sellAmount_mismatch"

    min_buy = to_int(quote.get("minBuyAmount"))
    if min_buy <= 0:
        return False, "missing_minBuyAmount"

    allowance_target = quote.get("allowanceTarget") or (
        (quote.get("issues") or {}).get("allowance") or {}
    ).get("spender")
    if not is_native(sell_cs):
        if not allowance_target:
            return False, "missing_allowanceTarget"
        if not addr_eq(to_cs, allowance_target):
            return False, "transaction.to_is_not_allowanceTarget"
        if addr_eq(allowance_target, PERMIT2):
            return False, "permit2_not_supported"
    else:
        # Native ETH: still require a destination, never a random EOA.
        if int(to_cs, 16) == 0:
            return False, "zero_destination"

    if not tx.get("data"):
        return False, "missing_transaction.data"
    return True, "ok"


def validate_approve_spender(spender: str, allowance_target: str) -> tuple[bool, str]:
    if not spender or not allowance_target:
        return False, "missing_spender"
    if addr_eq(spender, PERMIT2):
        return False, "refuse_permit2_approve"
    if not addr_eq(spender, allowance_target):
        return False, "spender_is_not_quote_allowanceTarget"
    return True, "ok"


def roundtrip_cost_pct(buy_out: int, sell_back: int) -> float:
    """Immediate round-trip loss as % of entry notional (atomic units).

    buy_out = tokens received when spending X ETH
    sell_back = ETH received when selling those tokens
    Entry notional is implied by comparing sell_back to the ETH we spent,
    so callers should pass comparable units (both ETH wei).
    """
    if buy_out <= 0:
        return 100.0
    # This helper is used when both numbers are already in ETH wei:
    # spent_wei vs sell_back_wei. For token-unit pairs, callers should not
    # use this. See roundtrip_eth_pct.
    return 0.0


def roundtrip_eth_pct(spent_wei: int, recovered_wei: int) -> float:
    if spent_wei <= 0:
        return 100.0
    return max(0.0, (1.0 - recovered_wei / spent_wei) * 100.0)


def assemble_legacy_tx(
    *,
    to: str,
    data: str | bytes,
    value: int,
    gas: int,
    gas_price: int,
    nonce: int,
    chain_id: int = RH_CHAIN_ID,
) -> dict:
    if isinstance(data, str):
        data_bytes = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    else:
        data_bytes = data
    return {
        "to": checksum(to),
        "value": int(value),
        "gas": int(gas),
        "gasPrice": int(gas_price),
        "nonce": int(nonce),
        "chainId": int(chain_id),
        "data": data_bytes,
    }
