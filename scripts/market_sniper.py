"""Market Sniper — early-token + breakout shadow strategies with market pulse.

Honest caveat: true launch sniping (same-block buys) is won by MEV bots on
dedicated RPC nodes measured in milliseconds. This module tests the edges that
ARE reachable with a polling bot:

  fresh_listing — tokens minutes old that already attracted real liquidity.
                  Lottery-style: wide stop, 2x target, 12h max hold.
  breakout      — tokens with real liquidity accelerating +25%..+300% in the
                  last hour. Excludes launch-pump spikes (+60,000%) that are
                  already over.

Both record hypothetical $25 "shadow" positions at detection-time market price
plus slippage — the fill a real copier of this signal would get. Exits are
stop-loss, take-profit, or max-hold, marked to market every cycle.

It also logs a market-wide pulse each cycle (breadth of the top-100 volume
tokens, new-listing rate) so the digest shows what the whole market is doing.

Writes only to its own tables (sniper_trades, market_pulse). Never touches
signals, paper_trades, or whale trace.

Usage:
    python scripts/market_sniper.py            # one scan cycle
    python scripts/market_sniper.py --report   # print performance report
    python scripts/market_sniper.py --report --send  # push report to Telegram
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
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
    SNIPER_BREAKOUT,
    SNIPER_ENABLED,
    SNIPER_FRESH,
    SNIPER_MAX_NEW_PER_CYCLE,
    SNIPER_REENTRY_COOLDOWN_HOURS,
    SNIPER_SHADOW_SIZE_USD,
    SNIPER_SLIPPAGE_PCT,
)
from whale_trace import fetch_price, fetch_prices  # noqa: E402

load_dotenv(dotenv_path=ROOT / ".env")

DB_PATH = ROOT / "data" / "memecoins.db"
BIRDEYE_BASE = "https://public-api.birdeye.so"
BIRDEYE_KEY = os.getenv("BIRDEYE_API_KEY", "")
SLIP = SNIPER_SLIPPAGE_PCT / 100.0

STRATEGIES = {"fresh_listing": SNIPER_FRESH, "breakout": SNIPER_BREAKOUT}


# ---------------------------------------------------------------- schema ----

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sniper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL,
            address TEXT NOT NULL,
            symbol TEXT,
            opened_at INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            shadow_size_usd REAL NOT NULL,
            tokens_held REAL NOT NULL,
            entry_meta_json TEXT,
            closed_at INTEGER,
            close_price REAL,
            close_reason TEXT,
            pnl_usd REAL,
            pnl_pct REAL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sniper_open
        ON sniper_trades(strategy, address, closed_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_pulse (
            timestamp INTEGER PRIMARY KEY,
            new_listings_seen INTEGER,
            top100_median_change_1h REAL,
            top100_pct_gainers_1h REAL,
            breakout_candidates INTEGER
        )
    """)
    conn.commit()
    conn.close()


def _headers() -> dict:
    return {"X-API-KEY": BIRDEYE_KEY, "x-chain": "solana", "accept": "application/json"}


# ------------------------------------------------------------- scanners -----

def scan_fresh_listings(client: httpx.Client) -> list[dict]:
    """New tokens with real liquidity, younger than the age cap."""
    try:
        r = client.get(
            f"{BIRDEYE_BASE}/defi/v2/tokens/new_listing",
            headers=_headers(), params={"limit": 20}, timeout=20.0,  # API caps at 20
        )
        if r.status_code != 200:
            print(f"[fresh] HTTP {r.status_code}", file=sys.stderr)
            return []
        items = r.json().get("data", {}).get("items", [])
    except Exception as e:
        print(f"[fresh] {type(e).__name__}: {e}", file=sys.stderr)
        return []

    now = time.time()
    out = []
    for it in items:
        liq = float(it.get("liquidity") or 0)
        if liq < SNIPER_FRESH["min_liquidity"]:
            continue
        added = it.get("liquidityAddedAt")
        if not added:
            continue
        try:
            age_min = (now - datetime.fromisoformat(added).replace(
                tzinfo=timezone.utc).timestamp()) / 60
        except ValueError:
            continue
        if age_min > SNIPER_FRESH["max_age_minutes"] or age_min < 0:
            continue
        out.append({
            "address": it.get("address", ""),
            "symbol": it.get("symbol") or "?",
            "meta": {"liquidity": liq, "age_min": round(age_min, 1),
                     "source": it.get("source")},
        })
    return out


def scan_breakouts(client: httpx.Client) -> tuple[list[dict], int]:
    """Tokens accelerating in the last hour, inside the sane band.
    Returns (candidates, total_matching) — total feeds the market pulse."""
    cfg = SNIPER_BREAKOUT
    try:
        r = client.get(
            f"{BIRDEYE_BASE}/defi/v3/token/list",
            headers=_headers(),
            params={
                "sort_by": "price_change_1h_percent", "sort_type": "desc",
                "min_liquidity": cfg["min_liquidity"],
                "min_volume_1h_usd": cfg["min_volume_1h"],
                "limit": 50,
            },
            timeout=30.0,
        )
        if r.status_code != 200:
            print(f"[breakout] HTTP {r.status_code}", file=sys.stderr)
            return [], 0
        items = r.json().get("data", {}).get("items", [])
    except Exception as e:
        print(f"[breakout] {type(e).__name__}: {e}", file=sys.stderr)
        return [], 0

    out = []
    for it in items:
        chg = it.get("price_change_1h_percent")
        price = it.get("price")
        if chg is None or not price:
            continue
        if not (cfg["min_change_1h"] <= chg <= cfg["max_change_1h"]):
            continue
        out.append({
            "address": it.get("address", ""),
            "symbol": it.get("symbol") or "?",
            "price": float(price),
            "meta": {"change_1h": round(chg, 1),
                     "liquidity": round(float(it.get("liquidity") or 0)),
                     "volume_1h": round(float(it.get("volume_1h_usd") or 0))},
        })
    return out, len(out)


def market_breadth(client: httpx.Client) -> tuple[float | None, float | None]:
    """(median 1h change, pct gainers) across top-100 tokens by 24h volume."""
    try:
        r = client.get(
            f"{BIRDEYE_BASE}/defi/v3/token/list",
            headers=_headers(),
            params={"sort_by": "volume_24h_usd", "sort_type": "desc",
                    "min_liquidity": 50_000, "limit": 100},
            timeout=30.0,
        )
        if r.status_code != 200:
            return None, None
        items = r.json().get("data", {}).get("items", [])
        changes = [it.get("price_change_1h_percent") for it in items
                   if it.get("price_change_1h_percent") is not None]
        if not changes:
            return None, None
        median = statistics.median(changes)
        pct_gainers = 100 * sum(1 for c in changes if c > 0) / len(changes)
        return median, pct_gainers
    except Exception:
        return None, None


# ------------------------------------------------------------ trade logic ---

def is_blocked(conn: sqlite3.Connection, strategy: str, addr: str) -> bool:
    """True if already open, or closed too recently to re-enter."""
    if conn.execute("""
        SELECT 1 FROM sniper_trades
        WHERE strategy = ? AND address = ? AND closed_at IS NULL
    """, (strategy, addr)).fetchone():
        return True
    cooldown_cutoff = int(time.time()) - SNIPER_REENTRY_COOLDOWN_HOURS * 3600
    if conn.execute("""
        SELECT 1 FROM sniper_trades
        WHERE strategy = ? AND address = ? AND closed_at >= ?
    """, (strategy, addr, cooldown_cutoff)).fetchone():
        return True
    return False


def open_count(conn: sqlite3.Connection, strategy: str) -> int:
    return conn.execute("""
        SELECT COUNT(*) FROM sniper_trades
        WHERE strategy = ? AND closed_at IS NULL
    """, (strategy,)).fetchone()[0]


def open_shadow(conn: sqlite3.Connection, strategy: str, addr: str, sym: str,
                market_price: float, meta: dict) -> dict:
    entry = market_price * (1 + SLIP)
    tokens = SNIPER_SHADOW_SIZE_USD / entry
    conn.execute("""
        INSERT INTO sniper_trades
            (strategy, address, symbol, opened_at, entry_price,
             shadow_size_usd, tokens_held, entry_meta_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (strategy, addr, sym, int(time.time()), entry,
          SNIPER_SHADOW_SIZE_USD, tokens, json.dumps(meta)))
    return {"strategy": strategy, "symbol": sym, "price": entry, "meta": meta}


def close_shadow(conn: sqlite3.Connection, trade_id: int, price: float,
                 reason: str) -> dict:
    row = conn.execute("""
        SELECT strategy, symbol, entry_price, shadow_size_usd, tokens_held
        FROM sniper_trades WHERE id = ?
    """, (trade_id,)).fetchone()
    strategy, sym, entry, size, tokens = row
    pnl = tokens * price - size
    pnl_pct = (pnl / size * 100) if size else 0.0
    conn.execute("""
        UPDATE sniper_trades
        SET closed_at = ?, close_price = ?, close_reason = ?,
            pnl_usd = ?, pnl_pct = ?
        WHERE id = ?
    """, (int(time.time()), price, reason, pnl, pnl_pct, trade_id))
    return {"strategy": strategy, "symbol": sym, "pnl_usd": pnl,
            "pnl_pct": pnl_pct, "reason": reason}


def scan_cycle() -> dict:
    init_db()
    now = int(time.time())
    opens: list[dict] = []
    closes: list[dict] = []
    conn = sqlite3.connect(DB_PATH)

    with httpx.Client() as client:
        # --- scan and open ---
        fresh = scan_fresh_listings(client)
        breakouts, n_breakout_candidates = scan_breakouts(client)

        candidates = {
            "fresh_listing": fresh,
            "breakout": breakouts,
        }
        for strategy, cands in candidates.items():
            cfg = STRATEGIES[strategy]
            opened_this_cycle = 0
            for c in cands:
                if opened_this_cycle >= SNIPER_MAX_NEW_PER_CYCLE:
                    break
                if open_count(conn, strategy) >= cfg["max_open"]:
                    break
                if not c["address"] or is_blocked(conn, strategy, c["address"]):
                    continue
                price = c.get("price") or fetch_price(client, c["address"])
                if not price:
                    continue
                opens.append(open_shadow(
                    conn, strategy, c["address"], c["symbol"], price, c["meta"]))
                opened_this_cycle += 1
            conn.commit()

        # --- mark open shadows to market ---
        open_rows = conn.execute("""
            SELECT id, strategy, address, opened_at, shadow_size_usd, tokens_held
            FROM sniper_trades WHERE closed_at IS NULL
        """).fetchall()
        prices = fetch_prices(client, list({r[2] for r in open_rows}))
        for trade_id, strategy, addr, opened_at, size, tokens in open_rows:
            market = prices.get(addr)
            if not market:
                continue
            cfg = STRATEGIES[strategy]
            exit_price = market * (1 - SLIP)
            pnl_pct = (tokens * exit_price - size) / size * 100
            if pnl_pct <= -cfg["stop_pct"]:
                closes.append(close_shadow(conn, trade_id, exit_price, "STOP_LOSS"))
            elif pnl_pct >= cfg["tp_pct"]:
                closes.append(close_shadow(conn, trade_id, exit_price, "TAKE_PROFIT"))
            elif opened_at < now - cfg["max_hold_hours"] * 3600:
                closes.append(close_shadow(conn, trade_id, exit_price, "MAX_HOLD"))
        conn.commit()

        # --- market pulse ---
        median_chg, pct_gainers = market_breadth(client)
        conn.execute("""
            INSERT OR REPLACE INTO market_pulse
            (timestamp, new_listings_seen, top100_median_change_1h,
             top100_pct_gainers_1h, breakout_candidates)
            VALUES (?, ?, ?, ?, ?)
        """, (now, len(fresh), median_chg, pct_gainers, n_breakout_candidates))
        conn.commit()

    conn.close()
    return {"opens": opens, "closes": closes,
            "pulse": {"median_1h": median_chg, "pct_gainers": pct_gainers,
                      "fresh_seen": len(fresh)}}


# --------------------------------------------------------------- reporting --

def build_report() -> str:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    lines = ["<b>Market Sniper (shadow, not real trades)</b>"]

    for strategy in STRATEGIES:
        r = conn.execute("""
            SELECT COUNT(*) n,
                   SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins,
                   COALESCE(SUM(pnl_usd), 0) total,
                   COALESCE(AVG(pnl_pct), 0) avg_pct
            FROM sniper_trades
            WHERE strategy = ? AND closed_at IS NOT NULL
        """, (strategy,)).fetchone()
        n = r["n"] or 0
        wr = (100 * (r["wins"] or 0) / n) if n else 0.0
        n_open = conn.execute("""
            SELECT COUNT(*) FROM sniper_trades
            WHERE strategy = ? AND closed_at IS NULL
        """, (strategy,)).fetchone()[0]
        lines.append(f"  {strategy}: {n} closed, {wr:.0f}% WR, "
                     f"{r['total']:+.2f} (avg {r['avg_pct']:+.1f}%), {n_open} open")

    pulse = conn.execute("""
        SELECT * FROM market_pulse ORDER BY timestamp DESC LIMIT 1
    """).fetchone()
    if pulse:
        med = pulse["top100_median_change_1h"]
        pg = pulse["top100_pct_gainers_1h"]
        lines.append("")
        lines.append("<b>Market pulse (top-100 by volume)</b>")
        if med is not None:
            mood = "risk-on" if pg and pg > 55 else ("risk-off" if pg and pg < 45 else "mixed")
            lines.append(f"  1h median: {med:+.1f}%  |  gainers: {pg:.0f}%  ({mood})")
        lines.append(f"  fresh listings passing filter: {pulse['new_listings_seen']}"
                     f"  |  breakout candidates: {pulse['breakout_candidates']}")
    conn.close()
    return "\n".join(lines)


def notify(summary: dict) -> None:
    if not telegram_notifier.is_configured():
        return
    for o in summary["opens"]:
        meta = ", ".join(f"{k}={v}" for k, v in o["meta"].items())
        telegram_notifier.send(
            f"<b>SNIPER — shadow open</b>\n"
            f"<b>{o['symbol']}</b> ({o['strategy']}) @ {o['price']:.10f}\n"
            f"{meta}\n(not a real trade)",
            silent=True,
        )
    for c in summary["closes"]:
        sign = "+" if c["pnl_usd"] >= 0 else ""
        telegram_notifier.send(
            f"<b>SNIPER — shadow closed</b>\n"
            f"<b>{c['symbol']}</b> ({c['strategy']})\n"
            f"P&L: {sign}${c['pnl_usd']:.2f} ({sign}{c['pnl_pct']:.1f}%)\n"
            f"Reason: {c['reason']}",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Market Sniper shadow strategies.")
    parser.add_argument("--report", action="store_true", help="Print report only.")
    parser.add_argument("--send", action="store_true", help="Also push to Telegram.")
    args = parser.parse_args()

    if args.report:
        report = build_report()
        print(report.replace("<b>", "").replace("</b>", ""))
        if args.send:
            ok = telegram_notifier.send(report)
            print(f"\n[telegram] sent: {ok}")
        return

    if not SNIPER_ENABLED:
        print("Market Sniper disabled in whale_config.py.")
        return
    if not BIRDEYE_KEY:
        print("BIRDEYE_API_KEY missing from .env.", file=sys.stderr)
        sys.exit(1)

    summary = scan_cycle()
    print(f"Opens: {len(summary['opens'])}  Closes: {len(summary['closes'])}")
    for o in summary["opens"]:
        print(f"  OPEN  [{o['strategy']}] {o['symbol']} @ {o['price']:.10f}  {o['meta']}")
    for c in summary["closes"]:
        print(f"  CLOSE [{c['strategy']}] {c['symbol']} {c['pnl_usd']:+.2f} ({c['reason']})")
    p = summary["pulse"]
    if p["median_1h"] is not None:
        print(f"Pulse: top-100 median 1h {p['median_1h']:+.1f}%, "
              f"{p['pct_gainers']:.0f}% gainers, {p['fresh_seen']} fresh listings")
    notify(summary)


if __name__ == "__main__":
    main()
