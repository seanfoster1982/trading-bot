"""Whale Trace — shadow-follow specific high-PnL Solana wallets.

This is a SEPARATE analysis from the whale_copy strategy. It:
  1. Pulls Birdeye's weekly top-PnL trader leaderboard and keeps wallets with
     real (realized) profit and a meaningful trade count.
  2. Polls each traced wallet's recent swaps.
  3. Records "shadow trades": when a traced wallet buys a token, we log a
     hypothetical $25 position at their fill price; when they sell (or after
     24h), we close it and record the P&L we WOULD have made copying them.

It writes only to its own tables (whale_wallets, whale_trace_trades) and never
touches signals or paper_trades, so it cannot affect the live whale_copy run.

Usage:
    python scripts/whale_trace.py            # one trace cycle
    python scripts/whale_trace.py --report   # print performance report only
    python scripts/whale_trace.py --report --send   # push report to Telegram
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import telegram_notifier  # noqa: E402
from whale_config import (  # noqa: E402
    WHALE_TRACE_ENABLED,
    WHALE_TRACE_LEADERBOARD_REFRESH_HOURS,
    WHALE_TRACE_MAX_HOLD_HOURS,
    WHALE_TRACE_MAX_WALLETS,
    WHALE_TRACE_MIN_BUY_USD,
    WHALE_TRACE_MIN_REALIZED_PNL,
    WHALE_TRACE_MIN_TRADES_1W,
    WHALE_TRACE_SHADOW_SIZE_USD,
)

load_dotenv(dotenv_path=ROOT / ".env")

DB_PATH = ROOT / "data" / "memecoins.db"
BIRDEYE_BASE = "https://public-api.birdeye.so"
BIRDEYE_KEY = os.getenv("BIRDEYE_API_KEY", "")

# Tokens we treat as "cash" — a swap from these into something else is a buy.
CASH_MINTS = {
    "So11111111111111111111111111111111111111112",   # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


# ---------------------------------------------------------------- schema ----

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS whale_wallets (
            wallet TEXT PRIMARY KEY,
            pnl_1w REAL,
            realized_pnl_1w REAL,
            trade_count_1w INTEGER,
            volume_1w REAL,
            active INTEGER NOT NULL DEFAULT 1,
            first_seen INTEGER NOT NULL,
            last_refreshed INTEGER NOT NULL,
            last_checked_at INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS whale_trace_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            address TEXT NOT NULL,
            symbol TEXT,
            opened_at INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            whale_buy_usd REAL,
            shadow_size_usd REAL NOT NULL,
            tokens_held REAL NOT NULL,
            entry_tx TEXT,
            closed_at INTEGER,
            close_price REAL,
            close_reason TEXT,
            close_tx TEXT,
            pnl_usd REAL,
            pnl_pct REAL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_trace_open
        ON whale_trace_trades(wallet, address, closed_at)
    """)
    conn.commit()
    conn.close()


# ------------------------------------------------------------ birdeye API ---

def _headers() -> dict:
    return {"X-API-KEY": BIRDEYE_KEY, "x-chain": "solana", "accept": "application/json"}


def fetch_leaderboard(client: httpx.Client, pages: int = 3) -> list[dict]:
    """Weekly top-PnL wallets from Birdeye (10 per page)."""
    items: list[dict] = []
    for page in range(pages):
        try:
            r = client.get(
                f"{BIRDEYE_BASE}/trader/gainers-losers",
                headers=_headers(),
                params={
                    "type": "1W", "sort_by": "PnL", "sort_type": "desc",
                    "offset": page * 10, "limit": 10,
                },
                timeout=20.0,
            )
            if r.status_code != 200:
                print(f"[leaderboard] HTTP {r.status_code}: {r.text[:150]}", file=sys.stderr)
                break
            items.extend(r.json().get("data", {}).get("items", []))
        except Exception as e:
            print(f"[leaderboard] {type(e).__name__}: {e}", file=sys.stderr)
            break
        time.sleep(0.25)
    return items


def fetch_wallet_swaps(client: httpx.Client, wallet: str, after_time: int) -> list[dict]:
    """Recent swap txs for a wallet, oldest first."""
    try:
        r = client.get(
            f"{BIRDEYE_BASE}/trader/txs/seek_by_time",
            headers=_headers(),
            params={"address": wallet, "after_time": after_time, "limit": 100},
            timeout=20.0,
        )
        if r.status_code != 200:
            print(f"[swaps {wallet[:8]}] HTTP {r.status_code}", file=sys.stderr)
            return []
        items = r.json().get("data", {}).get("items", [])
        items = [it for it in items if it.get("tx_type") == "swap"]
        items.sort(key=lambda it: it.get("block_unix_time", 0))
        return items
    except Exception as e:
        print(f"[swaps {wallet[:8]}] {type(e).__name__}: {e}", file=sys.stderr)
        return []


def fetch_price(client: httpx.Client, address: str) -> float | None:
    try:
        r = client.get(
            f"{BIRDEYE_BASE}/defi/price",
            headers=_headers(),
            params={"address": address},
            timeout=15.0,
        )
        if r.status_code != 200:
            return None
        val = r.json().get("data", {}).get("value")
        return float(val) if val else None
    except Exception:
        return None


# ------------------------------------------------------- swap classification

def classify_swap(item: dict) -> tuple[str, str, str, float, float] | None:
    """Return (side, token_address, token_symbol, token_amount, usd_value)
    where side is 'buy' or 'sell'. None if not a clean cash<->token swap."""
    base, quote = item.get("base") or {}, item.get("quote") or {}
    base_addr, quote_addr = base.get("address", ""), quote.get("address", "")
    base_cash, quote_cash = base_addr in CASH_MINTS, quote_addr in CASH_MINTS
    if base_cash == quote_cash:  # token-to-token or cash-to-cash — skip
        return None
    cash, token = (base, quote) if base_cash else (quote, base)
    usd = float(item.get("volume_usd") or 0)
    token_amt = abs(float(token.get("ui_change_amount") or token.get("ui_amount") or 0))
    if usd <= 0 or token_amt <= 0:
        return None
    # Wallet receives the token => buy; sends the token => sell.
    side = "buy" if token.get("type_swap") == "to" else "sell"
    return side, token.get("address", ""), token.get("symbol", "?"), token_amt, usd


# ------------------------------------------------------------ trace logic ---

def refresh_leaderboard_if_stale(client: httpx.Client, conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT MAX(last_refreshed) FROM whale_wallets").fetchone()
    last = row[0] or 0
    if time.time() - last < WHALE_TRACE_LEADERBOARD_REFRESH_HOURS * 3600:
        return

    print("Refreshing whale leaderboard from Birdeye...")
    items = fetch_leaderboard(client)
    now = int(time.time())
    qualified = [
        it for it in items
        if (it.get("trade_count") or 0) >= WHALE_TRACE_MIN_TRADES_1W
        and (it.get("realized_pnl") or 0) >= WHALE_TRACE_MIN_REALIZED_PNL
    ]
    qualified.sort(key=lambda it: it.get("realized_pnl") or 0, reverse=True)
    keep = qualified[:WHALE_TRACE_MAX_WALLETS]

    # Deactivate wallets that fell off; keep their trade history.
    conn.execute("UPDATE whale_wallets SET active = 0")
    for it in keep:
        w = it["address"]
        conn.execute("""
            INSERT INTO whale_wallets
                (wallet, pnl_1w, realized_pnl_1w, trade_count_1w, volume_1w,
                 active, first_seen, last_refreshed, last_checked_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, 0)
            ON CONFLICT(wallet) DO UPDATE SET
                pnl_1w = excluded.pnl_1w,
                realized_pnl_1w = excluded.realized_pnl_1w,
                trade_count_1w = excluded.trade_count_1w,
                volume_1w = excluded.volume_1w,
                active = 1,
                last_refreshed = excluded.last_refreshed
        """, (w, it.get("pnl"), it.get("realized_pnl"), it.get("trade_count"),
              it.get("volume"), now, now))
    conn.commit()
    print(f"  {len(items)} leaderboard wallets fetched, {len(qualified)} qualified, tracing {len(keep)}")


def open_shadow(conn: sqlite3.Connection, wallet: str, addr: str, sym: str,
                price: float, whale_usd: float, ts: int, tx: str) -> bool:
    dup = conn.execute("""
        SELECT 1 FROM whale_trace_trades
        WHERE wallet = ? AND address = ? AND closed_at IS NULL
    """, (wallet, addr)).fetchone()
    if dup:
        return False
    tokens = WHALE_TRACE_SHADOW_SIZE_USD / price
    conn.execute("""
        INSERT INTO whale_trace_trades
            (wallet, address, symbol, opened_at, entry_price, whale_buy_usd,
             shadow_size_usd, tokens_held, entry_tx)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (wallet, addr, sym, ts, price, whale_usd,
          WHALE_TRACE_SHADOW_SIZE_USD, tokens, tx))
    return True


def close_shadow(conn: sqlite3.Connection, trade_id: int, price: float,
                 reason: str, ts: int, tx: str | None = None) -> dict:
    row = conn.execute("""
        SELECT entry_price, shadow_size_usd, tokens_held, symbol, wallet
        FROM whale_trace_trades WHERE id = ?
    """, (trade_id,)).fetchone()
    entry, size, tokens, sym, wallet = row
    pnl = tokens * price - size
    pnl_pct = (pnl / size * 100) if size else 0.0
    conn.execute("""
        UPDATE whale_trace_trades
        SET closed_at = ?, close_price = ?, close_reason = ?, close_tx = ?,
            pnl_usd = ?, pnl_pct = ?
        WHERE id = ?
    """, (ts, price, reason, tx, pnl, pnl_pct, trade_id))
    return {"symbol": sym, "wallet": wallet, "pnl_usd": pnl, "pnl_pct": pnl_pct,
            "reason": reason}


def trace_cycle() -> dict:
    """One full trace pass. Returns summary of opens/closes."""
    init_db()
    opens: list[dict] = []
    closes: list[dict] = []
    conn = sqlite3.connect(DB_PATH)
    with httpx.Client() as client:
        refresh_leaderboard_if_stale(client, conn)

        wallets = conn.execute("""
            SELECT wallet, last_checked_at FROM whale_wallets WHERE active = 1
        """).fetchall()
        print(f"Tracing {len(wallets)} wallets...")

        for wallet, last_checked in wallets:
            # First pass: only look back 2h so we don't backfill week-old trades.
            after = last_checked if last_checked > 0 else int(time.time()) - 2 * 3600
            swaps = fetch_wallet_swaps(client, wallet, after)
            newest_ts = last_checked
            for it in swaps:
                ts_ = int(it.get("block_unix_time") or 0)
                newest_ts = max(newest_ts, ts_)
                parsed = classify_swap(it)
                if not parsed:
                    continue
                side, addr, sym, token_amt, usd = parsed
                price = usd / token_amt
                tx = it.get("tx_hash", "")
                if side == "buy" and usd >= WHALE_TRACE_MIN_BUY_USD:
                    if open_shadow(conn, wallet, addr, sym, price, usd, ts_, tx):
                        opens.append({"symbol": sym, "wallet": wallet,
                                      "price": price, "whale_usd": usd})
                elif side == "sell":
                    open_row = conn.execute("""
                        SELECT id FROM whale_trace_trades
                        WHERE wallet = ? AND address = ? AND closed_at IS NULL
                    """, (wallet, addr)).fetchone()
                    if open_row:
                        closes.append(close_shadow(
                            conn, open_row[0], price, "WHALE_SOLD", ts_, tx))
            conn.execute("UPDATE whale_wallets SET last_checked_at = ? WHERE wallet = ?",
                         (newest_ts, wallet))
            conn.commit()
            time.sleep(0.25)

        # Force-close shadows held past the max window at current market price.
        stale_cutoff = int(time.time()) - WHALE_TRACE_MAX_HOLD_HOURS * 3600
        stale = conn.execute("""
            SELECT id, address FROM whale_trace_trades
            WHERE closed_at IS NULL AND opened_at < ?
        """, (stale_cutoff,)).fetchall()
        for trade_id, addr in stale:
            price = fetch_price(client, addr)
            if price:
                closes.append(close_shadow(
                    conn, trade_id, price, "MAX_HOLD_24H", int(time.time())))
            time.sleep(0.2)
        conn.commit()

    conn.close()
    return {"opens": opens, "closes": closes}


# --------------------------------------------------------------- reporting --

def build_report() -> str:
    """HTML-formatted (Telegram-ready) whale trace performance report."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    closed = conn.execute("""
        SELECT COUNT(*) n,
               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins,
               COALESCE(SUM(pnl_usd), 0) total
        FROM whale_trace_trades WHERE closed_at IS NOT NULL
    """).fetchone()
    n, wins, total = closed["n"] or 0, closed["wins"] or 0, closed["total"] or 0.0
    wr = (100 * wins / n) if n else 0.0

    lines = ["<b>Whale Trace (shadow copy, not real trades)</b>"]
    lines.append(f"Closed shadows: {n}  |  WR: {wr:.0f}%  |  P&L: {total:+.2f}")

    per_wallet = conn.execute("""
        SELECT wallet, COUNT(*) n,
               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins,
               COALESCE(SUM(pnl_usd), 0) total
        FROM whale_trace_trades WHERE closed_at IS NOT NULL
        GROUP BY wallet ORDER BY total DESC LIMIT 5
    """).fetchall()
    if per_wallet:
        lines.append("")
        lines.append("<b>Top traced wallets</b>")
        for r in per_wallet:
            w_wr = (100 * (r["wins"] or 0) / r["n"]) if r["n"] else 0
            lines.append(f"  {r['wallet'][:8]}..: {r['n']} closed, "
                         f"{w_wr:.0f}% WR, {r['total']:+.2f}")

    open_rows = conn.execute("""
        SELECT symbol, wallet, opened_at, whale_buy_usd FROM whale_trace_trades
        WHERE closed_at IS NULL ORDER BY opened_at DESC
    """).fetchall()
    lines.append("")
    if open_rows:
        lines.append(f"<b>Open shadows ({len(open_rows)})</b>")
        for r in open_rows[:8]:
            age_h = (time.time() - r["opened_at"]) / 3600
            lines.append(f"  {r['symbol']} via {r['wallet'][:8]}.. "
                         f"(whale put ${r['whale_buy_usd']:,.0f}, {age_h:.1f}h ago)")
    else:
        lines.append("<b>Open shadows:</b> none")

    n_active = conn.execute(
        "SELECT COUNT(*) FROM whale_wallets WHERE active = 1").fetchone()[0]
    lines.append(f"\nTracing {n_active} wallets from Birdeye weekly PnL leaderboard.")
    conn.close()
    return "\n".join(lines)


def notify(summary: dict) -> None:
    if not telegram_notifier.is_configured():
        return
    for o in summary["opens"]:
        telegram_notifier.send(
            f"<b>WHALE TRACE — wallet bought</b>\n"
            f"<b>{o['symbol']}</b> @ {o['price']:.8f}\n"
            f"Wallet {o['wallet'][:8]}.. spent ${o['whale_usd']:,.0f}\n"
            f"Shadow position: ${WHALE_TRACE_SHADOW_SIZE_USD:.0f} (not a real trade)",
            silent=True,
        )
    for c in summary["closes"]:
        sign = "+" if c["pnl_usd"] >= 0 else ""
        telegram_notifier.send(
            f"<b>WHALE TRACE — shadow closed</b>\n"
            f"<b>{c['symbol']}</b> via {c['wallet'][:8]}..\n"
            f"P&L: {sign}${c['pnl_usd']:.2f} ({sign}{c['pnl_pct']:.1f}%)\n"
            f"Reason: {c['reason']}",
            silent=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Whale Trace — shadow-follow top wallets.")
    parser.add_argument("--report", action="store_true", help="Print report only, no API calls.")
    parser.add_argument("--send", action="store_true", help="Also push to Telegram.")
    args = parser.parse_args()

    if args.report:
        report = build_report()
        print(report.replace("<b>", "").replace("</b>", ""))
        if args.send:
            ok = telegram_notifier.send(report)
            print(f"\n[telegram] sent: {ok}")
        return

    if not WHALE_TRACE_ENABLED:
        print("Whale Trace disabled in whale_config.py.")
        return
    if not BIRDEYE_KEY:
        print("BIRDEYE_API_KEY missing from .env.", file=sys.stderr)
        sys.exit(1)

    summary = trace_cycle()
    print(f"\nOpens: {len(summary['opens'])}  Closes: {len(summary['closes'])}")
    for o in summary["opens"]:
        print(f"  OPEN  {o['symbol']} via {o['wallet'][:8]}.. (whale ${o['whale_usd']:,.0f})")
    for c in summary["closes"]:
        print(f"  CLOSE {c['symbol']} {c['pnl_usd']:+.2f} ({c['reason']})")
    notify(summary)


if __name__ == "__main__":
    main()
