"""LIVE trader — real-money execution with a hard lifetime budget.

Authorized by the user on 2026-07-26: invest $25 of real funds when the bot
finds a REALLY good candidate. Everything here is belt-and-braces:

  - Lifetime budget: SUM of all live buys can never exceed LIVE_BUDGET_USD.
  - One open position at a time (LIVE_MAX_OPEN).
  - Entry criteria (ALL must hold, far stricter than the shadow strategies):
      * Signal: >= LIVE_WHALE_CONFLUENCE_MIN distinct traced whale wallets
        bought the same token within LIVE_CONFLUENCE_WINDOW_MIN minutes
        (whale trace shadows), OR a bb_bounce full-checklist entry fired in
        the last 30 minutes.
      * Fresh token safety audit score <= LIVE_SAFETY_MAX_SCORE (30 vs the
        shadow gate's 50) with zero hard blocks.
      * Liquidity >= LIVE_MIN_LIQUIDITY.
      * Wallet holds enough SOL (trade + fee buffer). If not, the executor
        stays armed and tells Telegram it is waiting for funding.
  - Execution via Jupiter (quote -> swap -> sign locally -> send). The
    private key never leaves this machine.
  - Exit ladder mirrors the shadow strategies: recover initial stake at 2x,
    hard stop at -LIVE_STOP_PCT%, break-even stop after +30%, max-hold close.
  - Every action (buy, derisk, sell, skip-due-to-funding) is pushed to
    Telegram as an audible alert.

Usage:
    python scripts/live_trader.py                 # one full cycle
    python scripts/live_trader.py --status        # print state, no actions
    python scripts/live_trader.py --test-plumbing <mint>
        # quote + build + sign a $1 swap WITHOUT sending it (validates the
        # whole execution path with zero risk)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# Under pythonw.exe (scheduled tasks) the std streams are None entirely.
if sys.stdout is None or sys.stderr is None:
    _devnull = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or _devnull
    sys.stderr = sys.stderr or _devnull
else:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(dotenv_path=ROOT / ".env")

import telegram_notifier  # noqa: E402
from token_safety import check_token  # noqa: E402
from whale_config import (  # noqa: E402
    LIVE_BUDGET_USD,
    LIVE_CONFLUENCE_WINDOW_MIN,
    LIVE_ENABLED,
    LIVE_FEE_BUFFER_SOL,
    LIVE_MAX_HOLD_HOURS,
    LIVE_MAX_OPEN,
    LIVE_MAX_REALIZED_LOSS_USD,
    LIVE_MIN_HOURS_BETWEEN_BUYS,
    LIVE_MIN_LIQUIDITY,
    LIVE_SAFETY_MAX_SCORE,
    LIVE_SLIPPAGE_BPS,
    LIVE_STOP_PCT,
    LIVE_TRADE_USD,
    LIVE_WHALE_CONFLUENCE_MIN,
    SHADOW_BREAK_EVEN_TRIGGER_PCT,
    SHADOW_TAKE_INITIAL_MULT,
)

DB_PATH = ROOT / "data" / "memecoins.db"
BIRDEYE_BASE = "https://public-api.birdeye.so"
BIRDEYE_KEY = os.getenv("BIRDEYE_API_KEY", "")
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000

# Jupiter free-tier endpoints (fallback to legacy host).
JUP_HOSTS = ["https://lite-api.jup.ag/swap/v1", "https://quote-api.jup.ag/v6"]


# ------------------------------------------------------------------ setup ---

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    # An empty live_trades scaffold from a retired design may exist; move it
    # aside rather than colliding with the new schema.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(live_trades)")]
    if cols and "usd_spent" not in cols:
        conn.execute("ALTER TABLE live_trades RENAME TO live_trades_legacy")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT NOT NULL,
            symbol TEXT,
            source TEXT NOT NULL,
            opened_at INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            usd_spent REAL NOT NULL,
            sol_spent REAL NOT NULL,
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


def get_keypair():
    from solders.keypair import Keypair
    raw = (os.getenv("SOLANA_PRIVATE_KEY") or "").strip()
    if not raw:
        return None
    try:
        if raw.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(raw)))
        return Keypair.from_base58_string(raw)
    except Exception:
        return None


def rpc_url() -> str:
    helius = os.getenv("HELIUS_API_KEY", "")
    if helius:
        return f"https://mainnet.helius-rpc.com/?api-key={helius}"
    return "https://api.mainnet-beta.solana.com"


def _be_headers() -> dict:
    return {"X-API-KEY": BIRDEYE_KEY, "x-chain": "solana",
            "accept": "application/json"}


# -------------------------------------------------------------- chain I/O ---

def get_sol_balance(client: httpx.Client, pubkey: str) -> float:
    r = client.post(rpc_url(), json={"jsonrpc": "2.0", "id": 1,
                                     "method": "getBalance",
                                     "params": [pubkey]}, timeout=20.0)
    return (r.json().get("result", {}).get("value", 0)) / LAMPORTS


def get_token_balance_raw(client: httpx.Client, pubkey: str, mint: str) -> tuple[int, int]:
    """Return (raw_amount, decimals) for the wallet's holding of mint."""
    r = client.post(rpc_url(), json={
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [pubkey, {"mint": mint}, {"encoding": "jsonParsed"}],
    }, timeout=20.0)
    total, decimals = 0, 0
    for acct in r.json().get("result", {}).get("value", []):
        amt = acct["account"]["data"]["parsed"]["info"]["tokenAmount"]
        total += int(amt["amount"])
        decimals = int(amt["decimals"])
    return total, decimals


def fetch_price(client: httpx.Client, address: str) -> float | None:
    try:
        r = client.get(f"{BIRDEYE_BASE}/defi/price", headers=_be_headers(),
                       params={"address": address}, timeout=20.0)
        if r.status_code != 200:
            return None
        return (r.json().get("data") or {}).get("value")
    except Exception:
        return None


def fetch_liquidity(client: httpx.Client, address: str) -> float:
    try:
        r = client.get(f"{BIRDEYE_BASE}/defi/token_overview",
                       headers=_be_headers(),
                       params={"address": address}, timeout=20.0)
        if r.status_code != 200:
            return 0.0
        return float((r.json().get("data") or {}).get("liquidity") or 0)
    except Exception:
        return 0.0


# ------------------------------------------------------ jupiter execution ---

def jup_quote(client: httpx.Client, in_mint: str, out_mint: str,
              raw_amount: int) -> dict | None:
    params = {"inputMint": in_mint, "outputMint": out_mint,
              "amount": str(raw_amount), "slippageBps": str(LIVE_SLIPPAGE_BPS)}
    for host in JUP_HOSTS:
        try:
            r = client.get(f"{host}/quote", params=params, timeout=25.0)
            if r.status_code == 200:
                q = r.json()
                if q.get("outAmount"):
                    q["_host"] = host
                    return q
        except Exception:
            continue
    return None


def jup_build_swap(client: httpx.Client, quote: dict, pubkey: str) -> str | None:
    """Return base64 unsigned transaction, or None."""
    host = quote.pop("_host", JUP_HOSTS[0])
    body = {"quoteResponse": quote, "userPublicKey": pubkey,
            "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto"}
    try:
        r = client.post(f"{host}/swap", json=body, timeout=25.0)
        if r.status_code != 200:
            print(f"[jup swap] HTTP {r.status_code}: {r.text[:200]}")
            return None
        return r.json().get("swapTransaction")
    except Exception as e:
        print(f"[jup swap] {type(e).__name__}: {e}")
        return None


def sign_tx(tx_b64: str, keypair) -> bytes:
    from solders.message import to_bytes_versioned
    from solders.transaction import VersionedTransaction
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    sig = keypair.sign_message(to_bytes_versioned(tx.message))
    return bytes(VersionedTransaction.populate(tx.message, [sig]))


def send_tx(client: httpx.Client, signed: bytes) -> str | None:
    r = client.post(rpc_url(), json={
        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
        "params": [base64.b64encode(signed).decode(),
                   {"encoding": "base64", "skipPreflight": False,
                    "maxRetries": 3}],
    }, timeout=40.0)
    d = r.json()
    if "error" in d:
        print(f"[send] RPC error: {json.dumps(d['error'])[:300]}")
        return None
    return d.get("result")


def confirm_tx(client: httpx.Client, sig: str, tries: int = 10) -> bool:
    for _ in range(tries):
        time.sleep(3)
        r = client.post(rpc_url(), json={
            "jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
            "params": [[sig], {"searchTransactionHistory": True}],
        }, timeout=20.0)
        st = (r.json().get("result", {}).get("value") or [None])[0]
        if st:
            if st.get("err"):
                print(f"[confirm] tx failed on chain: {st['err']}")
                return False
            if st.get("confirmationStatus") in ("confirmed", "finalized"):
                return True
    return False


def execute_swap(client: httpx.Client, keypair, in_mint: str, out_mint: str,
                 raw_amount: int) -> tuple[str | None, dict | None]:
    """Full quote->build->sign->send->confirm. Returns (tx_sig, quote)."""
    quote = jup_quote(client, in_mint, out_mint, raw_amount)
    if not quote:
        print("[swap] no route/quote")
        return None, None
    tx_b64 = jup_build_swap(client, dict(quote), str(keypair.pubkey()))
    if not tx_b64:
        return None, quote
    sig = send_tx(client, sign_tx(tx_b64, keypair))
    if not sig:
        return None, quote
    if not confirm_tx(client, sig):
        print(f"[swap] tx not confirmed: {sig}")
        return None, quote
    return sig, quote


# -------------------------------------------------------------- candidates ---

def find_candidates(conn: sqlite3.Connection) -> list[dict]:
    """Really-good candidates from the shadow systems, best first."""
    now = int(time.time())
    out: list[dict] = []

    # Whale confluence: N distinct traced wallets bought the same token
    # inside the window. The single strongest copyable signal we have.
    rows = conn.execute("""
        SELECT address, symbol, COUNT(DISTINCT wallet) AS n
        FROM whale_trace_trades
        WHERE opened_at >= ? AND entry_mode = 'market'
        GROUP BY address HAVING n >= ?
        ORDER BY n DESC
    """, (now - LIVE_CONFLUENCE_WINDOW_MIN * 60,
          LIVE_WHALE_CONFLUENCE_MIN)).fetchall()
    for addr, sym, n in rows:
        out.append({"address": addr, "symbol": sym,
                    "source": f"whale_confluence_x{n}"})

    # bb_bounce: the user's full-checklist technical setup, fired recently.
    rows = conn.execute("""
        SELECT address, symbol FROM sniper_trades
        WHERE strategy = 'bb_bounce' AND opened_at >= ?
    """, (now - 30 * 60,)).fetchall()
    for addr, sym in rows:
        out.append({"address": addr, "symbol": sym, "source": "bb_bounce"})

    return out


def already_traded(conn: sqlite3.Connection, address: str) -> bool:
    return conn.execute("SELECT 1 FROM live_trades WHERE address = ?",
                        (address,)).fetchone() is not None


def budget_left(conn: sqlite3.Connection) -> float:
    spent = conn.execute(
        "SELECT COALESCE(SUM(usd_spent), 0) FROM live_trades").fetchone()[0]
    return LIVE_BUDGET_USD - spent


def open_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM live_trades WHERE closed_at IS NULL"
    ).fetchone()[0]


def realized_pnl(conn: sqlite3.Connection) -> float:
    return conn.execute("""
        SELECT COALESCE(SUM(pnl_usd), 0) FROM live_trades
        WHERE closed_at IS NOT NULL
    """).fetchone()[0]


def last_buy_at(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(opened_at), 0) FROM live_trades").fetchone()[0]


# ------------------------------------------------------------- buy / sell ---

def try_buy(client: httpx.Client, conn: sqlite3.Connection, keypair,
            cand: dict) -> dict | None:
    addr, sym = cand["address"], cand["symbol"]

    safety = check_token(client, addr, sym, use_cache=False)
    if safety is None or safety["hard_block"] or safety["score"] > LIVE_SAFETY_MAX_SCORE:
        reason = "no data" if safety is None else f"score {safety['score']:.0f}"
        print(f"  [live] {sym}: fails strict safety ({reason})")
        return None

    liq = fetch_liquidity(client, addr)
    if liq < LIVE_MIN_LIQUIDITY:
        print(f"  [live] {sym}: liquidity ${liq:,.0f} < ${LIVE_MIN_LIQUIDITY:,.0f}")
        return None

    sol_price = fetch_price(client, SOL_MINT)
    if not sol_price:
        return None
    pubkey = str(keypair.pubkey())
    balance = get_sol_balance(client, pubkey)
    sol_needed = LIVE_TRADE_USD / sol_price + LIVE_FEE_BUFFER_SOL
    if balance < sol_needed:
        msg = (f"LIVE EXECUTOR ARMED but wallet underfunded.\n"
               f"Candidate: {sym} ({cand['source']}, safety "
               f"{safety['score']:.0f}, liq ${liq:,.0f})\n"
               f"Need {sol_needed:.4f} SOL, have {balance:.4f}.\n"
               f"Fund {pubkey} to enable the buy.")
        print(f"  [live] {msg}")
        telegram_notifier.send_message(msg, silent=False)
        return None

    lamports = int(LIVE_TRADE_USD / sol_price * LAMPORTS)
    print(f"  [live] BUYING {sym}: ${LIVE_TRADE_USD} "
          f"({lamports / LAMPORTS:.4f} SOL), source {cand['source']}")
    sig, quote = execute_swap(client, keypair, SOL_MINT, addr, lamports)
    if not sig:
        telegram_notifier.send_message(
            f"LIVE BUY FAILED for {sym} — swap did not execute. "
            f"No funds spent beyond fees.", silent=False)
        return None

    price = fetch_price(client, addr) or 0.0
    raw_amt, decimals = get_token_balance_raw(client, pubkey, addr)
    tokens = raw_amt / (10 ** decimals) if raw_amt else (
        float(quote.get("outAmount", 0)) / (10 ** 9))
    now = int(time.time())
    conn.execute("""
        INSERT INTO live_trades
        (address, symbol, source, opened_at, entry_price, usd_spent,
         sol_spent, tokens, buy_tx)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (addr, sym, cand["source"], now, price, LIVE_TRADE_USD,
          lamports / LAMPORTS, tokens, sig))
    conn.commit()
    telegram_notifier.send_message(
        f"LIVE BUY EXECUTED\n{sym} — ${LIVE_TRADE_USD:.0f} real money\n"
        f"Source: {cand['source']}\nSafety: {safety['score']:.0f}/100, "
        f"liq ${liq:,.0f}\nEntry ~${price:.8f}\ntx: {sig}", silent=False)
    return {"symbol": sym, "tx": sig}


def sell_tokens(client: httpx.Client, keypair, mint: str,
                fraction: float) -> tuple[str | None, float]:
    """Sell a fraction (0-1] of current holding back to SOL."""
    pubkey = str(keypair.pubkey())
    raw_amt, decimals = get_token_balance_raw(client, pubkey, mint)
    if raw_amt <= 0:
        return None, 0.0
    amount = int(raw_amt * fraction)
    if amount <= 0:
        return None, 0.0
    sig, _ = execute_swap(client, keypair, mint, SOL_MINT, amount)
    return sig, amount / (10 ** decimals)


def manage_open(client: httpx.Client, conn: sqlite3.Connection, keypair) -> None:
    now = int(time.time())
    rows = conn.execute("""
        SELECT id, address, symbol, opened_at, entry_price, usd_spent, tokens,
               took_initial_at, initial_out_usd, peak_pnl_pct
        FROM live_trades WHERE closed_at IS NULL
    """).fetchall()
    for (tid, addr, sym, opened_at, entry, usd_in, tokens,
         took_initial_at, initial_out, peak) in rows:
        price = fetch_price(client, addr)
        if not price or not entry:
            continue
        pct = (price / entry - 1) * 100
        peak = max(peak, pct)
        conn.execute("UPDATE live_trades SET peak_pnl_pct = ? WHERE id = ?",
                     (peak, tid))

        # 1) take-initial at 2x: sell enough to recover the full stake
        if not took_initial_at and price >= entry * SHADOW_TAKE_INITIAL_MULT:
            frac = min(0.95, usd_in / (tokens * price))
            sig, sold = sell_tokens(client, keypair, addr, frac)
            if sig:
                out_usd = sold * price
                conn.execute("""
                    UPDATE live_trades SET took_initial_at = ?,
                        initial_out_usd = ?, tokens = tokens - ?
                    WHERE id = ?
                """, (now, out_usd, sold, tid))
                conn.commit()
                telegram_notifier.send_message(
                    f"LIVE DERISK: {sym} hit 2x — sold ${out_usd:,.2f} to "
                    f"recover initial stake. Remainder rides risk-free.\n"
                    f"tx: {sig}", silent=False)
            continue

        close_reason = None
        if pct <= -LIVE_STOP_PCT:
            close_reason = "STOP_LOSS"
        elif took_initial_at is None and peak >= SHADOW_BREAK_EVEN_TRIGGER_PCT and pct <= 0:
            close_reason = "BREAK_EVEN_STOP"
        elif now - opened_at >= LIVE_MAX_HOLD_HOURS * 3600:
            close_reason = "MAX_HOLD"
        if not close_reason:
            continue

        sig, sold = sell_tokens(client, keypair, addr, 1.0)
        if not sig:
            telegram_notifier.send_message(
                f"LIVE SELL FAILED for {sym} ({close_reason}) — will retry "
                f"next cycle.", silent=False)
            continue
        out_usd = sold * price
        pnl = out_usd + initial_out - usd_in
        conn.execute("""
            UPDATE live_trades SET closed_at = ?, close_reason = ?,
                exit_price = ?, sell_tx = ?, pnl_usd = ?
            WHERE id = ?
        """, (now, close_reason, price, sig, pnl, tid))
        conn.commit()
        telegram_notifier.send_message(
            f"LIVE CLOSE: {sym} — {close_reason}\n"
            f"P&L: ${pnl:+,.2f} on ${usd_in:.0f} ({pct:+.1f}%)\ntx: {sig}",
            silent=False)


# ------------------------------------------------------------------ cycle ---

def cycle() -> None:
    if not LIVE_ENABLED:
        print("live trading disabled in whale_config")
        return
    init_db()
    keypair = get_keypair()
    if keypair is None:
        print("SOLANA_PRIVATE_KEY missing/unparseable — cannot trade")
        return
    conn = sqlite3.connect(DB_PATH, timeout=30)
    with httpx.Client() as client:
        manage_open(client, conn, keypair)

        left = budget_left(conn)
        if left < LIVE_TRADE_USD:
            print(f"lifetime budget exhausted (${left:.2f} left) — manage-only mode")
            conn.close()
            return
        if open_count(conn) >= LIVE_MAX_OPEN:
            print("max live positions open — manage-only mode")
            conn.close()
            return
        pnl = realized_pnl(conn)
        if pnl <= -LIVE_MAX_REALIZED_LOSS_USD:
            print(f"DRAWDOWN HALT: realized P&L ${pnl:+.2f} — no new buys")
            conn.close()
            return
        since_last = int(time.time()) - last_buy_at(conn)
        if last_buy_at(conn) and since_last < LIVE_MIN_HOURS_BETWEEN_BUYS * 3600:
            print(f"buy spacing: last buy {since_last / 3600:.1f}h ago "
                  f"(min {LIVE_MIN_HOURS_BETWEEN_BUYS}h) — manage-only mode")
            conn.close()
            return

        cands = find_candidates(conn)
        print(f"{len(cands)} candidate(s) from shadow systems")
        for cand in cands:
            if already_traded(conn, cand["address"]):
                continue
            if try_buy(client, conn, keypair, cand):
                break  # one buy per cycle max
    conn.close()


def status() -> None:
    init_db()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    kp = get_keypair()
    with httpx.Client() as client:
        bal = get_sol_balance(client, str(kp.pubkey())) if kp else 0.0
        sol_price = fetch_price(client, SOL_MINT) or 0.0
    print(f"Wallet: {kp.pubkey() if kp else 'NO KEY'}")
    print(f"SOL: {bal:.4f} (~${bal * sol_price:,.2f})")
    print(f"Budget left: ${budget_left(conn):.2f} of ${LIVE_BUDGET_USD:.2f}")
    print(f"Open live positions: {open_count(conn)} (max {LIVE_MAX_OPEN})")
    pnl = realized_pnl(conn)
    halt = " [DRAWDOWN HALT ACTIVE]" if pnl <= -LIVE_MAX_REALIZED_LOSS_USD else ""
    print(f"Realized P&L: ${pnl:+.2f}{halt}")
    for row in conn.execute("""
        SELECT symbol, source, opened_at, usd_spent, pnl_usd, close_reason
        FROM live_trades ORDER BY id"""):
        print(" ", row)
    conn.close()


def test_plumbing(mint: str) -> None:
    """Quote + build + sign a $1 swap but NEVER send it."""
    keypair = get_keypair()
    if keypair is None:
        print("no keypair")
        return
    with httpx.Client() as client:
        sol_price = fetch_price(client, SOL_MINT)
        if not sol_price:
            print("no SOL price")
            return
        lamports = int(1.0 / sol_price * LAMPORTS)
        print(f"quote: {lamports / LAMPORTS:.6f} SOL -> {mint[:8]}...")
        quote = jup_quote(client, SOL_MINT, mint, lamports)
        if not quote:
            print("FAIL: no quote")
            return
        print(f"  outAmount={quote.get('outAmount')} "
              f"priceImpact={quote.get('priceImpactPct')} host={quote.get('_host')}")
        tx_b64 = jup_build_swap(client, dict(quote), str(keypair.pubkey()))
        if not tx_b64:
            print("FAIL: no swap transaction")
            return
        signed = sign_tx(tx_b64, keypair)
        print(f"  signed tx: {len(signed)} bytes")
        print("PLUMBING OK — transaction built and signed (NOT sent)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--test-plumbing", metavar="MINT")
    args = ap.parse_args()
    if args.status:
        status()
    elif args.test_plumbing:
        test_plumbing(args.test_plumbing)
    else:
        cycle()


if __name__ == "__main__":
    main()
