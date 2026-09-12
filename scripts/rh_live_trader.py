"""Robinhood Chain live trader — 0x AllowanceHolder, ETH gas, hard caps.

This is a NEW executor. It does not use Jupiter or Solana keys.
RH_LIVE_ENABLED is True after a successful 2026-09-12 plumbing check.
--status and --test-plumbing never broadcast. A bare run (or the
Windows TradingBotRhLive task) will send 0x swaps.

Usage:
    python scripts/rh_live_trader.py --status
    python scripts/rh_live_trader.py --scan
    python scripts/rh_live_trader.py --test-plumbing
    python scripts/rh_live_trader.py                 # one cycle (no-op if disabled)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from eth_abi import decode
from eth_account import Account
from eth_utils import to_checksum_address, to_hex

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

if sys.stdout is None or sys.stderr is None:
    _devnull = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or _devnull
    sys.stderr = sys.stderr or _devnull
else:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

load_dotenv(dotenv_path=ROOT / ".env")

import rh_chain as rh  # noqa: E402
import telegram_notifier  # noqa: E402
from whale_config import (  # noqa: E402
    RH_BUDGET_USD,
    RH_EXPECTED_ADDRESS,
    RH_FEE_BUFFER_ETH,
    RH_LIVE_ENABLED,
    RH_MAX_HOLD_HOURS,
    RH_MAX_OPEN,
    RH_MAX_REALIZED_LOSS_USD,
    RH_MAX_ROUNDTRIP_COST_PCT,
    RH_MIN_HOURS_BETWEEN_BUYS,
    RH_MIN_LIQUIDITY,
    RH_MIN_VOLUME_1H,
    RH_SLIPPAGE_BPS,
    RH_STOP_PCT,
    RH_TRADE_USD,
    SHADOW_BREAK_EVEN_TRIGGER_PCT,
    SHADOW_TAKE_INITIAL_MULT,
)

DB_PATH = ROOT / "data" / "memecoins.db"
WATCHLIST_PATH = ROOT / "data" / "rh_watchlist.json"
DEXSCREENER = "https://api.dexscreener.com"
COINGECKO = "https://api.coingecko.com/api/v3/simple/price"


def _notify(text: str, *, cooldown_key: str | None = None, cooldown_s: int = 0) -> None:
    if cooldown_key:
        path = ROOT / "data" / "cache" / f"tg_{cooldown_key}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if cooldown_s and path.exists() and time.time() - path.stat().st_mtime < cooldown_s:
            return
        path.write_text("1", encoding="utf-8")
    telegram_notifier.send(text)


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rh_live_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            symbol TEXT,
            source TEXT NOT NULL,
            opened_at INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            usd_spent REAL NOT NULL,
            eth_spent REAL NOT NULL,
            tokens REAL NOT NULL,
            buy_tx TEXT,
            took_initial_at INTEGER,
            initial_out_usd REAL NOT NULL DEFAULT 0,
            peak_pnl_pct REAL NOT NULL DEFAULT 0,
            closed_at INTEGER,
            close_reason TEXT,
            exit_price REAL,
            sell_tx TEXT,
            pnl_usd REAL
        )
    """)
    conn.commit()
    conn.close()


def rpc_url() -> str:
    return os.getenv("RH_RPC_URL") or os.getenv("ROBINHOOD_RPC_URL") or rh.RH_RPC_DEFAULT


def zerox_headers() -> dict:
    key = (os.getenv("ZERO_EX_API_KEY") or os.getenv("ZEROX_API_KEY") or "").strip()
    h = {"0x-version": rh.ZEROX_VERSION, "accept": "application/json"}
    if key:
        h["0x-api-key"] = key
    return h


def has_0x_key() -> bool:
    return bool((os.getenv("ZERO_EX_API_KEY") or os.getenv("ZEROX_API_KEY") or "").strip())


def load_account():
    raw = (os.getenv("EVM_PRIVATE_KEY") or os.getenv("ETHEREUM_PRIVATE_KEY") or "").strip()
    if not raw:
        return None
    if not raw.startswith("0x"):
        raw = "0x" + raw
    try:
        return Account.from_key(raw)
    except Exception:
        return None


def rpc_call(client: httpx.Client, method: str, params: list):
    r = client.post(
        rpc_url(),
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"User-Agent": "trading-bot-rh/1.0"},
        timeout=25.0,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise RuntimeError(body["error"])
    return body.get("result")


def eth_balance(client: httpx.Client, address: str) -> float:
    wei = rh.to_int(rpc_call(client, "eth_getBalance", [rh.checksum(address), "latest"]))
    return wei / 1e18


def eth_price_usd(client: httpx.Client) -> float:
    try:
        r = client.get(COINGECKO, params={"ids": "ethereum", "vs_currencies": "usd"}, timeout=15.0)
        return float(r.json()["ethereum"]["usd"])
    except Exception:
        return 0.0


def eth_call(client: httpx.Client, to: str, data: str) -> str:
    return rpc_call(client, "eth_call", [{"to": rh.checksum(to), "data": data}, "latest"]) or "0x"


def erc20_balance(client: httpx.Client, token: str, owner: str) -> int:
    raw = eth_call(client, token, rh.calldata_balance_of(owner))
    if not raw or raw == "0x":
        return 0
    return int(raw, 16)


def erc20_decimals(client: httpx.Client, token: str) -> int:
    raw = eth_call(client, token, rh.calldata_decimals())
    if not raw or raw == "0x":
        return 18
    return int(raw, 16)


def erc20_symbol(client: httpx.Client, token: str) -> str:
    raw = eth_call(client, token, rh.calldata_symbol())
    if not raw or raw in ("0x", "0x0"):
        return "?"
    data = bytes.fromhex(raw[2:])
    try:
        if len(data) >= 64:
            return decode(["string"], data)[0]
    except Exception:
        pass
    try:
        return data.rstrip(b"\x00").decode("utf-8", errors="replace")
    except Exception:
        return "?"


def zerox_price(client: httpx.Client, sell: str, buy: str, sell_amount: int,
                 taker: str) -> dict | None:
    if not has_0x_key():
        return None
    params = {
        "chainId": str(rh.RH_CHAIN_ID),
        "sellToken": rh.checksum(sell) if not rh.is_native(sell) else rh.NATIVE_ETH,
        "buyToken": rh.checksum(buy) if not rh.is_native(buy) else rh.NATIVE_ETH,
        "sellAmount": str(int(sell_amount)),
        "taker": rh.checksum(taker),
        "slippageBps": str(RH_SLIPPAGE_BPS),
    }
    r = client.get(
        f"{rh.ZEROX_API}/swap/allowance-holder/price",
        params=params, headers=zerox_headers(), timeout=25.0,
    )
    if r.status_code != 200:
        print(f"[0x price] HTTP {r.status_code}: {r.text[:220]}")
        return None
    return r.json()


def zerox_quote(client: httpx.Client, sell: str, buy: str, sell_amount: int,
                 taker: str) -> dict | None:
    if not has_0x_key():
        print("[0x] ZERO_EX_API_KEY missing — cannot quote")
        return None
    params = {
        "chainId": str(rh.RH_CHAIN_ID),
        "sellToken": rh.NATIVE_ETH if rh.is_native(sell) else rh.checksum(sell),
        "buyToken": rh.NATIVE_ETH if rh.is_native(buy) else rh.checksum(buy),
        "sellAmount": str(int(sell_amount)),
        "taker": rh.checksum(taker),
        "slippageBps": str(RH_SLIPPAGE_BPS),
    }
    r = client.get(
        f"{rh.ZEROX_API}/swap/allowance-holder/quote",
        params=params, headers=zerox_headers(), timeout=25.0,
    )
    if r.status_code != 200:
        print(f"[0x quote] HTTP {r.status_code}: {r.text[:220]}")
        return None
    return r.json()


def sign_and_maybe_send(client: httpx.Client, account, tx_dict: dict,
                         send: bool) -> str | None:
    signed = account.sign_transaction(tx_dict)
    raw = signed.raw_transaction
    txh = to_hex(signed.hash)
    if not send:
        print(f"  signed (NOT sent) {txh}")
        return txh
    result = rpc_call(client, "eth_sendRawTransaction", [to_hex(raw)])
    print(f"  broadcast {result}")
    return result


def wait_receipt(client: httpx.Client, txh: str, tries: int = 20) -> dict | None:
    for _ in range(tries):
        time.sleep(2)
        rec = rpc_call(client, "eth_getTransactionReceipt", [txh])
        if rec:
            return rec
    return None


def send_quote_tx(client: httpx.Client, account, quote: dict, send: bool) -> str | None:
    tx = quote["transaction"]
    nonce = rh.to_int(rpc_call(
        client, "eth_getTransactionCount", [account.address, "pending"]))
    assembled = rh.assemble_legacy_tx(
        to=tx["to"],
        data=tx["data"],
        value=rh.to_int(tx.get("value")),
        gas=int(rh.to_int(tx.get("gas")) * 1.2),
        gas_price=rh.to_int(tx.get("gasPrice") or tx.get("maxFeePerGas")),
        nonce=nonce,
    )
    return sign_and_maybe_send(client, account, assembled, send)


def maybe_approve(client: httpx.Client, account, quote: dict, send: bool) -> bool:
    sell = quote.get("sellToken") or ""
    if rh.is_native(sell):
        return True
    issues = (quote.get("issues") or {}).get("allowance")
    if not issues:
        return True
    spender = issues.get("spender") or quote.get("allowanceTarget")
    ok, reason = rh.validate_approve_spender(spender, quote.get("allowanceTarget") or spender)
    if not ok:
        print(f"  [approve] refused: {reason}")
        return False
    amount = rh.to_int(quote.get("sellAmount"))
    nonce = rh.to_int(rpc_call(
        client, "eth_getTransactionCount", [account.address, "pending"]))
    gas_price = rh.to_int(rpc_call(client, "eth_gasPrice", []))
    assembled = rh.assemble_legacy_tx(
        to=sell,
        data=rh.calldata_approve(spender, amount),
        value=0,
        gas=80_000,
        gas_price=gas_price,
        nonce=nonce,
    )
    txh = sign_and_maybe_send(client, account, assembled, send)
    if not send:
        return True
    if not txh:
        return False
    rec = wait_receipt(client, txh)
    if not rec or rh.to_int(rec.get("status")) != 1:
        print("  [approve] tx failed")
        return False
    return True


def load_watchlist() -> list[str]:
    if not WATCHLIST_PATH.exists():
        return []
    try:
        data = json.loads(WATCHLIST_PATH.read_text())
        return [rh.checksum(a) for a in (data.get("tokens") or [])]
    except Exception:
        return []


def scan_dexscreener(client: httpx.Client) -> list[dict]:
    urls = [
        f"{DEXSCREENER}/token-pairs/v1/robinhood/{rh.WETH}",
        f"{DEXSCREENER}/token-pairs/v1/robinhood/{rh.USDG}",
        f"{DEXSCREENER}/latest/dex/search?q=robinhood",
        f"{DEXSCREENER}/token-boosts/latest/v1",
    ]
    found: dict[str, dict] = {}
    for url in urls:
        try:
            r = client.get(url, timeout=20.0)
            if r.status_code != 200:
                continue
            payload = r.json()
        except Exception:
            continue
        pairs = []
        if isinstance(payload, dict):
            pairs = payload.get("pairs") or []
        elif isinstance(payload, list):
            if payload and "tokenAddress" in payload[0]:
                for b in payload:
                    if str(b.get("chainId", "")).lower() != "robinhood":
                        continue
                    addr = b.get("tokenAddress")
                    if addr:
                        found.setdefault(addr.lower(), {
                            "address": addr,
                            "symbol": "?",
                            "name": (b.get("description") or "")[:80],
                            "liquidity": 0.0,
                            "volume_1h": 0.0,
                            "change_1h": 0.0,
                            "source": "dexscreener_boost",
                        })
                continue
            pairs = payload
        for p in pairs:
            if str(p.get("chainId", "")).lower() != "robinhood":
                continue
            base = p.get("baseToken") or {}
            addr = base.get("address")
            if not addr:
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            prev = found.get(addr.lower())
            if prev and prev.get("liquidity", 0) >= liq:
                continue
            found[addr.lower()] = {
                "address": addr,
                "symbol": base.get("symbol") or "?",
                "name": base.get("name") or "",
                "liquidity": liq,
                "volume_1h": float((p.get("volume") or {}).get("h1") or 0),
                "change_1h": float((p.get("priceChange") or {}).get("h1") or 0),
                "price_usd": float(p.get("priceUsd") or 0),
                "source": "dexscreener",
            }
    out = []
    for item in found.values():
        blocked, reason = rh.is_blocked_token(
            item["address"], item.get("symbol"), item.get("name"))
        if blocked:
            continue
        if item.get("liquidity", 0) < RH_MIN_LIQUIDITY:
            continue
        if item.get("source") != "watchlist" and item.get("volume_1h", 0) < RH_MIN_VOLUME_1H:
            continue
        out.append(item)
    for w in load_watchlist():
        if w.lower() not in found:
            out.append({
                "address": w, "symbol": "?", "name": "watchlist",
                "liquidity": 0.0, "volume_1h": 0.0, "change_1h": 0.0,
                "source": "watchlist",
            })
    out.sort(key=lambda x: x.get("liquidity", 0), reverse=True)
    return out[:20]


def roundtrip_ok(client: httpx.Client, taker: str, token: str,
                 spend_wei: int) -> tuple[bool, float, str]:
    buy = zerox_price(client, rh.NATIVE_ETH, token, spend_wei, taker)
    if not buy:
        return False, 100.0, "no_buy_price"
    buy_out = rh.to_int(buy.get("buyAmount"))
    if buy_out <= 0:
        return False, 100.0, "zero_buy_amount"
    sell = zerox_price(client, token, rh.NATIVE_ETH, buy_out, taker)
    if not sell:
        return False, 100.0, "no_sell_price"
    recovered = rh.to_int(sell.get("buyAmount"))
    pct = rh.roundtrip_eth_pct(spend_wei, recovered)
    if pct > RH_MAX_ROUNDTRIP_COST_PCT:
        return False, pct, f"roundtrip_{pct:.2f}pct"
    return True, pct, "ok"


def budget_left(conn: sqlite3.Connection) -> float:
    spent = conn.execute(
        "SELECT COALESCE(SUM(usd_spent), 0) FROM rh_live_trades").fetchone()[0]
    return RH_BUDGET_USD - float(spent)


def open_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM rh_live_trades WHERE closed_at IS NULL"
    ).fetchone()[0]


def realized_pnl(conn: sqlite3.Connection) -> float:
    return float(conn.execute(
        "SELECT COALESCE(SUM(pnl_usd), 0) FROM rh_live_trades WHERE closed_at IS NOT NULL"
    ).fetchone()[0])


def last_buy_at(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COALESCE(MAX(opened_at), 0) FROM rh_live_trades").fetchone()[0])


def already_traded(conn: sqlite3.Connection, address: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM rh_live_trades WHERE address = ?",
        (rh.checksum(address),),
    ).fetchone() is not None


def try_buy(client: httpx.Client, conn: sqlite3.Connection, account,
            cand: dict, eth_px: float) -> dict | None:
    token = rh.checksum(cand["address"])
    blocked, reason = rh.is_blocked_token(token, cand.get("symbol"), cand.get("name"))
    if blocked and cand.get("source") != "watchlist":
        print(f"  skip {cand.get('symbol')}: {reason}")
        return None
    if already_traded(conn, token):
        return None
    if eth_px <= 0:
        return None
    spend_wei = int(RH_TRADE_USD / eth_px * 1e18)
    ok, rt, why = roundtrip_ok(client, account.address, token, spend_wei)
    if not ok:
        print(f"  skip {cand.get('symbol')}: {why}")
        return None
    quote = zerox_quote(client, rh.NATIVE_ETH, token, spend_wei, account.address)
    ok, why = rh.validate_quote(
        quote or {}, sell_token=rh.NATIVE_ETH, buy_token=token,
        taker=account.address, sell_amount=spend_wei)
    if not ok:
        print(f"  skip quote: {why}")
        return None
    print(f"  BUYING {cand.get('symbol')} ${RH_TRADE_USD:.2f} roundtrip={rt:.2f}%")
    if not maybe_approve(client, account, quote, send=True):
        return None
    # Re-quote after approve so the 0x order is fresh.
    quote = zerox_quote(client, rh.NATIVE_ETH, token, spend_wei, account.address)
    ok, why = rh.validate_quote(
        quote or {}, sell_token=rh.NATIVE_ETH, buy_token=token,
        taker=account.address, sell_amount=spend_wei)
    if not ok:
        print(f"  skip requote: {why}")
        return None
    txh = send_quote_tx(client, account, quote, send=True)
    if not txh:
        telegram_notifier.send(f"RH BUY FAILED {cand.get('symbol')} — not confirmed.")
        return None
    rec = wait_receipt(client, txh)
    if not rec or rh.to_int(rec.get("status")) != 1:
        telegram_notifier.send(f"RH BUY REVERTED {cand.get('symbol')} {txh}")
        return None
    decimals = erc20_decimals(client, token)
    raw_bal = erc20_balance(client, token, account.address)
    tokens = raw_bal / (10 ** decimals)
    buy_amt = rh.to_int(quote.get("buyAmount"))
    entry = (spend_wei / 1e18 * eth_px) / tokens if tokens else 0.0
    now = int(time.time())
    conn.execute("""
        INSERT INTO rh_live_trades
        (chain_id, address, symbol, source, opened_at, entry_price, usd_spent,
         eth_spent, tokens, buy_tx)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (rh.RH_CHAIN_ID, token, cand.get("symbol") or "?", cand.get("source"),
          now, entry, RH_TRADE_USD, spend_wei / 1e18, tokens, txh))
    conn.commit()
    telegram_notifier.send(
        f"<b>RH LIVE BUY</b>\n{cand.get('symbol')} ${RH_TRADE_USD:.0f}\n"
        f"{rh.RH_EXPLORER}/tx/{txh}\nround-trip est {rt:.2f}%"
    )
    return {"symbol": cand.get("symbol"), "tx": txh, "buyAmount": buy_amt}


def mark_price_usd(client: httpx.Client, token: str) -> float:
    try:
        r = client.get(
            f"{DEXSCREENER}/token-pairs/v1/robinhood/{rh.checksum(token)}",
            timeout=15.0)
        pairs = r.json() if r.status_code == 200 else []
        if isinstance(pairs, list):
            for p in pairs:
                px = float(p.get("priceUsd") or 0)
                if px:
                    return px
    except Exception:
        pass
    return 0.0


def manage_open(client: httpx.Client, conn: sqlite3.Connection, account,
                eth_px: float) -> None:
    now = int(time.time())
    rows = conn.execute("""
        SELECT id, address, symbol, opened_at, entry_price, usd_spent, tokens,
               took_initial_at, initial_out_usd, peak_pnl_pct
        FROM rh_live_trades WHERE closed_at IS NULL
    """).fetchall()
    for (tid, addr, sym, opened_at, entry, usd_in, tokens,
         took_initial, initial_out, peak) in rows:
        px = mark_price_usd(client, addr)
        if not px or not entry:
            continue
        pct = (px / entry - 1) * 100
        peak = max(peak or 0, pct)
        conn.execute("UPDATE rh_live_trades SET peak_pnl_pct = ? WHERE id = ?",
                     (peak, tid))
        conn.commit()
        close_reason = None
        if not took_initial and pct >= (SHADOW_TAKE_INITIAL_MULT - 1) * 100:
            # Partial exit: sell half via 0x (approx recover stake on a 2x).
            decimals = erc20_decimals(client, addr)
            raw = erc20_balance(client, addr, account.address)
            sell_raw = raw // 2
            if sell_raw > 0:
                q = zerox_quote(client, addr, rh.NATIVE_ETH, sell_raw, account.address)
                ok, why = rh.validate_quote(
                    q or {}, sell_token=addr, buy_token=rh.NATIVE_ETH,
                    taker=account.address, sell_amount=sell_raw)
                if ok and maybe_approve(client, account, q, send=True):
                    q = zerox_quote(client, addr, rh.NATIVE_ETH, sell_raw, account.address)
                    ok, why = rh.validate_quote(
                        q or {}, sell_token=addr, buy_token=rh.NATIVE_ETH,
                        taker=account.address, sell_amount=sell_raw)
                    if ok:
                        txh = send_quote_tx(client, account, q, send=True)
                        if txh:
                            rec = wait_receipt(client, txh)
                            if rec and rh.to_int(rec.get("status")) == 1:
                                out_usd = rh.to_int(q.get("buyAmount")) / 1e18 * eth_px
                                remaining = (raw - sell_raw) / (10 ** decimals)
                                conn.execute("""
                                    UPDATE rh_live_trades SET took_initial_at = ?,
                                        initial_out_usd = ?, tokens = ?
                                    WHERE id = ?
                                """, (now, out_usd, remaining, tid))
                                conn.commit()
                                telegram_notifier.send(
                                    f"RH DERISK {sym} 2x — recovered ~${out_usd:.2f}\n{txh}")
            continue
        if pct <= -RH_STOP_PCT:
            close_reason = "STOP_LOSS"
        elif took_initial is None and peak >= SHADOW_BREAK_EVEN_TRIGGER_PCT and pct <= 0:
            close_reason = "BREAK_EVEN_STOP"
        elif now - opened_at >= RH_MAX_HOLD_HOURS * 3600:
            close_reason = "MAX_HOLD"
        if not close_reason:
            continue
        decimals = erc20_decimals(client, addr)
        raw = erc20_balance(client, addr, account.address)
        if raw <= 0:
            continue
        q = zerox_quote(client, addr, rh.NATIVE_ETH, raw, account.address)
        ok, why = rh.validate_quote(
            q or {}, sell_token=addr, buy_token=rh.NATIVE_ETH,
            taker=account.address, sell_amount=raw)
        if not ok:
            telegram_notifier.send(f"RH SELL quote failed {sym} ({close_reason}): {why}")
            continue
        if not maybe_approve(client, account, q, send=True):
            continue
        q = zerox_quote(client, addr, rh.NATIVE_ETH, raw, account.address)
        ok, why = rh.validate_quote(
            q or {}, sell_token=addr, buy_token=rh.NATIVE_ETH,
            taker=account.address, sell_amount=raw)
        if not ok:
            continue
        txh = send_quote_tx(client, account, q, send=True)
        if not txh:
            telegram_notifier.send(f"RH SELL FAILED {sym} ({close_reason})")
            continue
        rec = wait_receipt(client, txh)
        if not rec or rh.to_int(rec.get("status")) != 1:
            telegram_notifier.send(f"RH SELL REVERTED {sym} {txh}")
            continue
        out_usd = rh.to_int(q.get("buyAmount")) / 1e18 * eth_px
        pnl = out_usd + (initial_out or 0) - usd_in
        conn.execute("""
            UPDATE rh_live_trades SET closed_at = ?, close_reason = ?,
                exit_price = ?, sell_tx = ?, pnl_usd = ?
            WHERE id = ?
        """, (now, close_reason, px, txh, pnl, tid))
        conn.commit()
        telegram_notifier.send(
            f"<b>RH CLOSE</b> {sym} {close_reason}\nPnL ${pnl:+.2f}\n{txh}")


def cycle() -> None:
    if not RH_LIVE_ENABLED:
        print("RH live trading disabled in whale_config (RH_LIVE_ENABLED=False)")
        return
    if not telegram_notifier.is_configured():
        print("WARNING: TELEGRAM_BOT_TOKEN/CHAT_ID missing — trades will not alert")
    init_db()
    account = load_account()
    if account is None:
        print("EVM_PRIVATE_KEY missing/unparseable — cannot trade")
        return
    if not rh.addr_eq(account.address, RH_EXPECTED_ADDRESS):
        print(f"ERROR: key derives {account.address}, expected {RH_EXPECTED_ADDRESS}")
        _notify(
            "RH live key mismatch — refusing to trade.",
            cooldown_key="key_mismatch",
            cooldown_s=6 * 3600,
        )
        return
    conn = sqlite3.connect(DB_PATH, timeout=30)
    with httpx.Client() as client:
        eth_px = eth_price_usd(client)
        manage_open(client, conn, account, eth_px)
        left = budget_left(conn)
        if left < RH_TRADE_USD:
            print(f"budget exhausted (${left:.2f} left)")
            conn.close()
            return
        if open_count(conn) >= RH_MAX_OPEN:
            print("max open — manage-only")
            conn.close()
            return
        pnl = realized_pnl(conn)
        if pnl <= -RH_MAX_REALIZED_LOSS_USD:
            print(f"DRAWDOWN HALT ${pnl:+.2f}")
            _notify(
                f"RH DRAWDOWN HALT ${pnl:+.2f} — no new buys.",
                cooldown_key="drawdown",
                cooldown_s=6 * 3600,
            )
            conn.close()
            return
        if last_buy_at(conn) and time.time() - last_buy_at(conn) < RH_MIN_HOURS_BETWEEN_BUYS * 3600:
            print("buy spacing — manage-only")
            conn.close()
            return
        bal = eth_balance(client, account.address)
        need = RH_TRADE_USD / eth_px + RH_FEE_BUFFER_ETH if eth_px else 999
        if bal < need:
            telegram_notifier.send(
                f"RH executor armed but underfunded. Have {bal:.4f} ETH, "
                f"need ~{need:.4f} ETH at ${eth_px:.0f}/ETH.")
            conn.close()
            return
        cands = scan_dexscreener(client)
        print(f"{len(cands)} candidate(s) after liquidity/ticker filters")
        for cand in cands:
            if try_buy(client, conn, account, cand, eth_px):
                break
    conn.close()


def status() -> None:
    init_db()
    account = load_account()
    with httpx.Client() as client:
        addr = account.address if account else RH_EXPECTED_ADDRESS
        try:
            bal = eth_balance(client, addr)
        except Exception as e:
            bal = 0.0
            print(f"RPC error: {e}")
        eth_px = eth_price_usd(client)
    print(f"Chain: Robinhood {rh.RH_CHAIN_ID}")
    print(f"Expected: {RH_EXPECTED_ADDRESS}")
    print(f"Derived:  {account.address if account else 'NO KEY IN THIS ENVIRONMENT'}")
    if account and not rh.addr_eq(account.address, RH_EXPECTED_ADDRESS):
        print("MISMATCH — refusing to trade this key.")
    print(f"ETH: {bal:.6f} (~${bal * eth_px:,.2f} @ ${eth_px:,.2f})")
    print(f"RH_LIVE_ENABLED: {RH_LIVE_ENABLED}")
    print(f"0x API key: {'yes' if has_0x_key() else 'NO — quotes disabled'}")
    print(
        f"Telegram: {'yes' if telegram_notifier.is_configured() else 'NO — fills will not alert'}"
    )
    conn = sqlite3.connect(DB_PATH, timeout=30)
    print(f"Budget left: ${budget_left(conn):.2f} of ${RH_BUDGET_USD:.2f}")
    print(f"Open: {open_count(conn)} (max {RH_MAX_OPEN})")
    print(f"Realized P&L: ${realized_pnl(conn):+.2f}")
    for row in conn.execute(
        "SELECT id, symbol, source, usd_spent, pnl_usd, close_reason, buy_tx "
        "FROM rh_live_trades ORDER BY id"
    ):
        print(" ", row)
    conn.close()
    print(f"Explorer: {rh.RH_EXPLORER}/address/{addr}")


def scan() -> None:
    with httpx.Client() as client:
        cands = scan_dexscreener(client)
    print(f"{len(cands)} tradable after filters (min liq ${RH_MIN_LIQUIDITY:,.0f})")
    for c in cands:
        print(f"  {c.get('symbol'):12} liq=${c.get('liquidity', 0):,.0f}  "
              f"vol1h=${c.get('volume_1h', 0):,.0f}  {c.get('address')}  [{c.get('source')}]")


def test_plumbing() -> None:
    """Quote ETH -> USDG for ~$1, validate, sign if key present, never send."""
    account = load_account()
    taker = account.address if account else RH_EXPECTED_ADDRESS
    with httpx.Client() as client:
        eth_px = eth_price_usd(client)
        if eth_px <= 0:
            print("no ETH price")
            return
        wei = int(1.0 / eth_px * 1e18)
        print(f"Dry-run quote: {wei} wei ETH (~$1) -> USDG {rh.USDG}")
        print(f"taker {taker}")
        quote = zerox_quote(client, rh.NATIVE_ETH, rh.USDG, wei, taker)
        ok, why = rh.validate_quote(
            quote or {}, sell_token=rh.NATIVE_ETH, buy_token=rh.USDG,
            taker=taker, sell_amount=wei)
        print(f"validate_quote: {ok} ({why})")
        if not quote:
            return
        tx = quote.get("transaction") or {}
        print(f"to: {tx.get('to')}")
        print(f"value: {tx.get('value')}")
        print(f"minBuyAmount: {quote.get('minBuyAmount')}")
        print(f"buyAmount: {quote.get('buyAmount')}")
        if not ok or account is None:
            return
        if not rh.addr_eq(account.address, RH_EXPECTED_ADDRESS):
            print("key mismatch — not signing")
            return
        send_quote_tx(client, account, quote, send=False)
        print("plumbing OK (signed, not broadcast)")


def build_report() -> str:
    init_db()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    n = conn.execute(
        "SELECT COUNT(*) FROM rh_live_trades WHERE closed_at IS NOT NULL"
    ).fetchone()[0]
    pnl = realized_pnl(conn)
    n_open = open_count(conn)
    conn.close()
    flag = "ARMED" if RH_LIVE_ENABLED else "DISABLED"
    return (
        f"<b>Robinhood Chain live ({flag})</b>\n"
        f"  wallet {RH_EXPECTED_ADDRESS[:8]}…  budget ${RH_BUDGET_USD:.0f} / "
        f"${RH_TRADE_USD:.0f} per trade\n"
        f"  closed {n}  open {n_open}  realized {pnl:+.2f}"
    )


def test_telegram() -> int:
    """Send a phone ping. Discovers chat id if the token is already in .env."""
    return 0 if telegram_notifier.setup(send_test=True) else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Robinhood Chain 0x live trader")
    p.add_argument("--status", action="store_true")
    p.add_argument("--scan", action="store_true")
    p.add_argument("--test-plumbing", action="store_true")
    p.add_argument("--test-telegram", action="store_true")
    args = p.parse_args()
    if args.status:
        status()
        return
    if args.scan:
        scan()
        return
    if args.test_plumbing:
        test_plumbing()
        return
    if args.test_telegram:
        raise SystemExit(test_telegram())
    cycle()


if __name__ == "__main__":
    main()
