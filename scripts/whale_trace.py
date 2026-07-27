"""Whale Trace — realistically shadow-copy high-PnL Solana wallets.

The primary strategy. It:
  1. Pulls Birdeye's weekly top-PnL trader leaderboard and keeps wallets with
     real (realized) profit and a meaningful trade count.
  2. Polls each traced wallet's recent swaps every cycle.
  3. Records "shadow trades" at COPYABLE prices: when a traced wallet buys a
     token, we log a hypothetical $25 position at the CURRENT market price
     (plus slippage) — the fill a real copier would get — not the whale's own
     fill. Exits: whale sells (at our detection-time market price), -50% stop,
     or 24h max hold.
  4. Culls wallets whose copyable results are consistently unprofitable, so
     the watchlist converges on wallets actually worth mirroring.

Rows recorded before this realism upgrade used the whale's own fill price and
are kept as entry_mode='fill' legacy data, reported separately — their P&L
overstates what a copier could earn (launch snipers fill at prices that exist
for milliseconds).

It writes only to its own tables (whale_wallets, whale_trace_trades) and never
touches signals or paper_trades.

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

# Memecoin symbols can contain arbitrary Unicode; don't let cp1252 consoles crash.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import telegram_notifier  # noqa: E402
from whale_config import (  # noqa: E402
    SAFETY_MAX_SCORE,
    SHADOW_BREAK_EVEN_TRIGGER_PCT,
    SHADOW_TAKE_INITIAL_MULT,
    WHALE_TRACE_CULL_MIN_CLOSED,
    WHALE_TRACE_ENABLED,
    WHALE_TRACE_LEADERBOARD_REFRESH_HOURS,
    WHALE_TRACE_MAX_CHASE_MULT,
    WHALE_TRACE_MAX_HOLD_HOURS,
    WHALE_TRACE_MAX_WALLETS,
    WHALE_TRACE_MIN_BUY_USD,
    WHALE_TRACE_MIN_REALIZED_PNL,
    WHALE_TRACE_MIN_TRADES_1W,
    WHALE_TRACE_SHADOW_SIZE_USD,
    WHALE_TRACE_SLIPPAGE_PCT,
    WHALE_TRACE_STOP_LOSS_PCT,
)
from token_safety import is_safe_to_buy  # noqa: E402

SLIP = WHALE_TRACE_SLIPPAGE_PCT / 100.0

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
    # Migrations for the realistic-copy upgrade. NULL entry_mode = legacy rows
    # recorded at the whale's own fill price.
    for ddl in (
        "ALTER TABLE whale_trace_trades ADD COLUMN entry_mode TEXT",
        "ALTER TABLE whale_trace_trades ADD COLUMN whale_fill_price REAL",
        "ALTER TABLE whale_wallets ADD COLUMN culled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE whale_trace_trades ADD COLUMN took_initial_at INTEGER",
        "ALTER TABLE whale_trace_trades ADD COLUMN initial_out_usd REAL NOT NULL DEFAULT 0",
        "ALTER TABLE whale_trace_trades ADD COLUMN peak_pnl_pct REAL NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    conn.close()


# ------------------------------------------------------------ birdeye API ---

def _headers() -> dict:
    return {"X-API-KEY": BIRDEYE_KEY, "x-chain": "solana", "accept": "application/json"}


def fetch_leaderboard(client: httpx.Client, pages: int = 10,
                      window: str = "1W") -> list[dict]:
    """Top-PnL wallets from Birdeye (10 per page). window: 1W or 30d.

    This is the same data behind Phantom's Explore > Top Traders list
    (Phantom sources token/trader data from Birdeye)."""
    items: list[dict] = []
    for page in range(pages):
        try:
            r = client.get(
                f"{BIRDEYE_BASE}/trader/gainers-losers",
                headers=_headers(),
                params={
                    "type": window, "sort_by": "PnL", "sort_type": "desc",
                    "offset": page * 10, "limit": 10,
                },
                timeout=20.0,
            )
            if r.status_code != 200:
                print(f"[leaderboard {window}] HTTP {r.status_code}: {r.text[:150]}", file=sys.stderr)
                break
            items.extend(r.json().get("data", {}).get("items", []))
        except Exception as e:
            print(f"[leaderboard {window}] {type(e).__name__}: {e}", file=sys.stderr)
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


def fetch_prices(client: httpx.Client, addresses: list[str]) -> dict[str, float]:
    """Batch current prices; returns {address: price} for those found."""
    if not addresses:
        return {}
    out: dict[str, float] = {}
    for i in range(0, len(addresses), 50):
        chunk = addresses[i:i + 50]
        try:
            r = client.get(
                f"{BIRDEYE_BASE}/defi/multi_price",
                headers=_headers(),
                params={"list_address": ",".join(chunk)},
                timeout=20.0,
            )
            if r.status_code != 200:
                continue
            data = r.json().get("data") or {}
            for addr, info in data.items():
                val = (info or {}).get("value")
                if val:
                    out[addr] = float(val)
        except Exception:
            continue
    return out


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

    print("Refreshing whale leaderboard from Birdeye (7d + 30d)...")
    now = int(time.time())
    # Merge the 7-day and 30-day boards (Phantom-style Top Traders windows).
    # 30-day realized PnL is scaled to a weekly rate so both windows compete
    # on the same qualification bar and sort order.
    merged: dict[str, dict] = {}
    for window, scale in (("1W", 1.0), ("30d", 7 / 30)):
        for it in fetch_leaderboard(client, window=window):
            w = it.get("address")
            if not w:
                continue
            weekly_pnl = (it.get("realized_pnl") or 0) * scale
            weekly_trades = (it.get("trade_count") or 0) * scale
            if (weekly_trades >= WHALE_TRACE_MIN_TRADES_1W
                    and weekly_pnl >= WHALE_TRACE_MIN_REALIZED_PNL):
                prev = merged.get(w)
                if prev is None or weekly_pnl > (prev.get("realized_pnl") or 0):
                    merged[w] = {**it, "realized_pnl": weekly_pnl,
                                 "trade_count": int(weekly_trades)}
    qualified = sorted(merged.values(),
                       key=lambda it: it.get("realized_pnl") or 0, reverse=True)
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
    # A culled wallet stays culled even if it re-enters the leaderboard.
    conn.execute("UPDATE whale_wallets SET active = 0 WHERE culled = 1")
    conn.commit()
    print(f"  {len(merged)} unique wallets qualified across 7d+30d boards, tracing {len(keep)}")


def open_shadow(conn: sqlite3.Connection, wallet: str, addr: str, sym: str,
                market_price: float, whale_fill: float, whale_usd: float,
                ts: int, tx: str) -> bool:
    """Open a shadow position at the copyable price: current market + slippage."""
    dup = conn.execute("""
        SELECT 1 FROM whale_trace_trades
        WHERE wallet = ? AND address = ? AND closed_at IS NULL
    """, (wallet, addr)).fetchone()
    if dup:
        return False
    entry = market_price * (1 + SLIP)
    tokens = WHALE_TRACE_SHADOW_SIZE_USD / entry
    conn.execute("""
        INSERT INTO whale_trace_trades
            (wallet, address, symbol, opened_at, entry_price, whale_buy_usd,
             shadow_size_usd, tokens_held, entry_tx, entry_mode, whale_fill_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'market', ?)
    """, (wallet, addr, sym, ts, entry, whale_usd,
          WHALE_TRACE_SHADOW_SIZE_USD, tokens, tx, whale_fill))
    return True


def close_shadow(conn: sqlite3.Connection, trade_id: int, price: float,
                 reason: str, ts: int, tx: str | None = None) -> dict:
    row = conn.execute("""
        SELECT entry_price, shadow_size_usd, tokens_held, symbol, wallet,
               initial_out_usd
        FROM whale_trace_trades WHERE id = ?
    """, (trade_id,)).fetchone()
    entry, size, tokens, sym, wallet, initial_out = row
    # Total P&L includes any principal already taken out at 2x.
    pnl = (initial_out or 0) + tokens * price - size
    pnl_pct = (pnl / size * 100) if size else 0.0
    conn.execute("""
        UPDATE whale_trace_trades
        SET closed_at = ?, close_price = ?, close_reason = ?, close_tx = ?,
            pnl_usd = ?, pnl_pct = ?
        WHERE id = ?
    """, (ts, price, reason, tx, pnl, pnl_pct, trade_id))
    return {"symbol": sym, "wallet": wallet, "pnl_usd": pnl, "pnl_pct": pnl_pct,
            "reason": reason}


def take_initial_out(conn: sqlite3.Connection, trade_id: int,
                     exit_price: float) -> dict:
    """Sell just enough tokens (at exit_price, slippage included) to recover
    the initial stake. Remaining tokens ride risk-free."""
    row = conn.execute("""
        SELECT symbol, wallet, shadow_size_usd, tokens_held
        FROM whale_trace_trades WHERE id = ?
    """, (trade_id,)).fetchone()
    sym, wallet, size, tokens = row
    tokens_sold = size / exit_price
    conn.execute("""
        UPDATE whale_trace_trades
        SET tokens_held = ?, took_initial_at = ?, initial_out_usd = ?
        WHERE id = ?
    """, (tokens - tokens_sold, int(time.time()), size, trade_id))
    return {"symbol": sym, "wallet": wallet, "recovered_usd": size,
            "price": exit_price}


def cull_bad_wallets(conn: sqlite3.Connection) -> list[str]:
    """Deactivate wallets whose copyable (market-mode) results are net losers."""
    rows = conn.execute("""
        SELECT wallet, COUNT(*) n, COALESCE(SUM(pnl_usd), 0) total
        FROM whale_trace_trades
        WHERE entry_mode = 'market' AND closed_at IS NOT NULL
        GROUP BY wallet
        HAVING n >= ? AND total < 0
    """, (WHALE_TRACE_CULL_MIN_CLOSED,)).fetchall()
    culled = [r[0] for r in rows]
    for w in culled:
        conn.execute(
            "UPDATE whale_wallets SET culled = 1, active = 0 WHERE wallet = ?", (w,))
    return culled


def trace_cycle() -> dict:
    """One full trace pass. Returns summary of opens/closes."""
    init_db()
    now = int(time.time())
    opens: list[dict] = []
    closes: list[dict] = []
    derisks: list[dict] = []
    conn = sqlite3.connect(DB_PATH)
    with httpx.Client() as client:
        refresh_leaderboard_if_stale(client, conn)

        wallets = conn.execute("""
            SELECT wallet, last_checked_at FROM whale_wallets
            WHERE active = 1 AND culled = 0
        """).fetchall()
        print(f"Tracing {len(wallets)} wallets...")

        for wallet, last_checked in wallets:
            # Never look back more than 2h — copying a stale buy is meaningless.
            after = max(last_checked, now - 2 * 3600)
            swaps = fetch_wallet_swaps(client, wallet, after)
            newest_ts = last_checked

            # Parse the whole batch first: if the whale already sold a token
            # later in this same batch, a real copier polling on our schedule
            # would never have entered — don't open just to eat slippage.
            batch = []
            for it in swaps:
                ts_ = int(it.get("block_unix_time") or 0)
                newest_ts = max(newest_ts, ts_)
                parsed = classify_swap(it)
                if parsed:
                    batch.append((ts_, it.get("tx_hash", ""), parsed))
            sold_later = {
                (p[1], t) for t, _, p in batch if p[0] == "sell"
            }  # (token_address, sell_ts)

            for ts_, tx, parsed in batch:
                side, addr, sym, token_amt, usd = parsed
                whale_fill = usd / token_amt
                if side == "buy" and usd >= WHALE_TRACE_MIN_BUY_USD:
                    if any(a == addr and sell_ts > ts_ for a, sell_ts in sold_later):
                        continue  # round-trip completed before we could act
                    already_open = conn.execute("""
                        SELECT 1 FROM whale_trace_trades
                        WHERE wallet = ? AND address = ? AND closed_at IS NULL
                    """, (wallet, addr)).fetchone()
                    if already_open:
                        continue
                    market = fetch_price(client, addr)
                    if not market:
                        continue
                    if market > whale_fill * WHALE_TRACE_MAX_CHASE_MULT:
                        # Token already ran away from the whale's fill — a real
                        # copier is too late. Don't chase.
                        continue
                    safe, safety = is_safe_to_buy(
                        client, addr, sym, max_score=SAFETY_MAX_SCORE)
                    if not safe:
                        reason = ("no security data" if safety is None
                                  else "; ".join(safety["flags"]) or
                                  f"score {safety['score']:.0f}")
                        print(f"  [safety] BLOCKED {sym}: {reason}")
                        continue
                    if open_shadow(conn, wallet, addr, sym, market, whale_fill,
                                   usd, ts_, tx):
                        opens.append({"symbol": sym, "wallet": wallet,
                                      "price": market * (1 + SLIP),
                                      "whale_usd": usd})
                elif side == "sell":
                    open_row = conn.execute("""
                        SELECT id FROM whale_trace_trades
                        WHERE wallet = ? AND address = ? AND closed_at IS NULL
                    """, (wallet, addr)).fetchone()
                    if open_row:
                        # Exit at the price WE see when detecting their sell.
                        market = fetch_price(client, addr)
                        if market:
                            closes.append(close_shadow(
                                conn, open_row[0], market * (1 - SLIP),
                                "WHALE_SOLD", now, tx))
            conn.execute("UPDATE whale_wallets SET last_checked_at = ? WHERE wallet = ?",
                         (newest_ts, wallet))
            conn.commit()
            time.sleep(0.25)

        # Mark open market-mode shadows to market. Exit ladder:
        #   1. 2x -> take initial out, remainder rides risk-free
        #   2. stop-loss (only while initial still at risk)
        #   3. break-even stop: peaked above trigger, fell back to flat
        #   4. max hold
        open_rows = conn.execute("""
            SELECT id, address, opened_at, shadow_size_usd, tokens_held,
                   took_initial_at, initial_out_usd, peak_pnl_pct
            FROM whale_trace_trades
            WHERE closed_at IS NULL AND entry_mode = 'market'
        """).fetchall()
        prices = fetch_prices(client, list({r[1] for r in open_rows}))
        max_hold_cutoff = now - WHALE_TRACE_MAX_HOLD_HOURS * 3600
        for (trade_id, addr, opened_at, size, tokens,
             took_initial, initial_out, peak) in open_rows:
            market = prices.get(addr)
            if not market:
                continue
            exit_price = market * (1 - SLIP)
            pnl_pct = ((initial_out or 0) + tokens * exit_price - size) / size * 100

            if pnl_pct > (peak or 0):
                conn.execute(
                    "UPDATE whale_trace_trades SET peak_pnl_pct = ? WHERE id = ?",
                    (pnl_pct, trade_id))
                peak = pnl_pct

            if not took_initial and tokens * exit_price >= size * SHADOW_TAKE_INITIAL_MULT:
                derisks.append(take_initial_out(conn, trade_id, exit_price))
                continue
            if not took_initial and pnl_pct <= -WHALE_TRACE_STOP_LOSS_PCT:
                closes.append(close_shadow(
                    conn, trade_id, exit_price, "STOP_LOSS", now))
            elif (not took_initial and peak >= SHADOW_BREAK_EVEN_TRIGGER_PCT
                    and pnl_pct <= 0):
                closes.append(close_shadow(
                    conn, trade_id, exit_price, "BREAK_EVEN_STOP", now))
            elif opened_at < max_hold_cutoff:
                closes.append(close_shadow(
                    conn, trade_id, exit_price, "MAX_HOLD_24H", now))
        conn.commit()

        culled = cull_bad_wallets(conn)
        if culled:
            print(f"Culled {len(culled)} wallet(s) with losing copyable records: "
                  + ", ".join(w[:8] + ".." for w in culled))
        conn.commit()

    conn.close()
    return {"opens": opens, "closes": closes, "derisks": derisks}


# --------------------------------------------------------------- reporting --

def build_report() -> str:
    """HTML-formatted (Telegram-ready) whale trace performance report."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    closed = conn.execute("""
        SELECT COUNT(*) n,
               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins,
               COALESCE(SUM(pnl_usd), 0) total,
               COALESCE(AVG(pnl_pct), 0) avg_pct
        FROM whale_trace_trades
        WHERE closed_at IS NOT NULL AND entry_mode = 'market'
    """).fetchone()
    n, wins, total = closed["n"] or 0, closed["wins"] or 0, closed["total"] or 0.0
    wr = (100 * wins / n) if n else 0.0

    lines = ["<b>Whale Trace (realistic copy simulation)</b>"]
    lines.append(f"Closed: {n}  |  WR: {wr:.0f}%  |  P&L: {total:+.2f}  "
                 f"|  avg {closed['avg_pct']:+.1f}%/trade")

    per_wallet = conn.execute("""
        SELECT wallet, COUNT(*) n,
               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins,
               COALESCE(SUM(pnl_usd), 0) total
        FROM whale_trace_trades
        WHERE closed_at IS NOT NULL AND entry_mode = 'market'
        GROUP BY wallet ORDER BY total DESC LIMIT 5
    """).fetchall()
    if per_wallet:
        lines.append("")
        lines.append("<b>Top traced wallets (copyable P&L)</b>")
        for r in per_wallet:
            w_wr = (100 * (r["wins"] or 0) / r["n"]) if r["n"] else 0
            lines.append(f"  {r['wallet'][:8]}..: {r['n']} closed, "
                         f"{w_wr:.0f}% WR, {r['total']:+.2f}")

    open_rows = conn.execute("""
        SELECT symbol, wallet, opened_at, whale_buy_usd, took_initial_at
        FROM whale_trace_trades
        WHERE closed_at IS NULL AND entry_mode = 'market'
        ORDER BY opened_at DESC
    """).fetchall()
    lines.append("")
    if open_rows:
        lines.append(f"<b>Open shadows ({len(open_rows)})</b>")
        for r in open_rows[:8]:
            age_h = (time.time() - r["opened_at"]) / 3600
            tag = " [risk-free]" if r["took_initial_at"] else ""
            lines.append(f"  {r['symbol']} via {r['wallet'][:8]}.. "
                         f"(whale put ${r['whale_buy_usd']:,.0f}, {age_h:.1f}h ago){tag}")
    else:
        lines.append("<b>Open shadows:</b> none")

    n_active = conn.execute(
        "SELECT COUNT(*) FROM whale_wallets WHERE active = 1 AND culled = 0"
    ).fetchone()[0]
    n_culled = conn.execute(
        "SELECT COUNT(*) FROM whale_wallets WHERE culled = 1").fetchone()[0]
    lines.append(f"\nTracing {n_active} wallets ({n_culled} culled for losing records).")

    legacy = conn.execute("""
        SELECT COUNT(*) n FROM whale_trace_trades
        WHERE closed_at IS NOT NULL AND (entry_mode IS NULL OR entry_mode = 'fill')
    """).fetchone()
    if legacy["n"]:
        lines.append(f"<i>({legacy['n']} legacy fill-price shadows excluded — "
                     f"not copyable prices)</i>")
    conn.close()
    return "\n".join(lines)


def notify(summary: dict) -> None:
    if not telegram_notifier.is_configured():
        return
    for d in summary.get("derisks", []):
        telegram_notifier.send(
            f"<b>WHALE TRACE — INITIAL OUT (2x)</b>\n"
            f"<b>{d['symbol']}</b> via {d['wallet'][:8]}..\n"
            f"Recovered ${d['recovered_usd']:.2f} stake @ {d['price']:.10f}\n"
            f"Remainder rides risk-free."
        )
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
    print(f"\nOpens: {len(summary['opens'])}  Closes: {len(summary['closes'])}  "
          f"Initial-outs: {len(summary['derisks'])}")
    for o in summary["opens"]:
        print(f"  OPEN  {o['symbol']} via {o['wallet'][:8]}.. (whale ${o['whale_usd']:,.0f})")
    for d in summary["derisks"]:
        print(f"  2X    {d['symbol']} initial ${d['recovered_usd']:.2f} out, runner rides free")
    for c in summary["closes"]:
        print(f"  CLOSE {c['symbol']} {c['pnl_usd']:+.2f} ({c['reason']})")
    notify(summary)


if __name__ == "__main__":
    main()
