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
import pandas as pd  # noqa: E402

from compute_indicators import (  # noqa: E402
    compute_bollinger,
    compute_macd,
    compute_stoch_rsi,
)
from whale_config import (  # noqa: E402
    ROADMAP_SIGNAL_DAYS,
    SAFETY_MAX_SCORE,
    SAFETY_MAX_SCORE_FRESH,
    SHADOW_BREAK_EVEN_TRIGGER_PCT,
    SHADOW_TAKE_INITIAL_MULT,
    SNIPER_BB_BOUNCE,
    SNIPER_BREAKOUT,
    SNIPER_ENABLED,
    SNIPER_FRESH,
    SNIPER_MAX_NEW_PER_CYCLE,
    SNIPER_REENTRY_COOLDOWN_HOURS,
    SNIPER_SHADOW_SIZE_USD,
    SNIPER_SLIPPAGE_PCT,
)
from whale_trace import CASH_MINTS, fetch_price, fetch_prices  # noqa: E402
from token_safety import is_safe_to_buy  # noqa: E402

load_dotenv(dotenv_path=ROOT / ".env")

DB_PATH = ROOT / "data" / "memecoins.db"
BIRDEYE_BASE = "https://public-api.birdeye.so"
BIRDEYE_KEY = os.getenv("BIRDEYE_API_KEY", "")
SLIP = SNIPER_SLIPPAGE_PCT / 100.0

STRATEGIES = {
    "fresh_listing": SNIPER_FRESH,
    "breakout": SNIPER_BREAKOUT,
    "bb_bounce": SNIPER_BB_BOUNCE,
}

ROADMAP_PATH = ROOT / "data" / "roadmap_events.json"

# Majors we never want the bounce strategy to shadow-trade.
MAJOR_SYMBOLS = {"SOL", "WSOL", "USDC", "USDT", "WBTC", "WETH", "CBBTC",
                 "JITOSOL", "MSOL", "BSOL", "JLP", "JUPSOL"}


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
    # Money-management columns: initial-out at 2x + break-even tracking.
    for ddl in (
        "ALTER TABLE sniper_trades ADD COLUMN took_initial_at INTEGER",
        "ALTER TABLE sniper_trades ADD COLUMN initial_out_usd REAL NOT NULL DEFAULT 0",
        "ALTER TABLE sniper_trades ADD COLUMN peak_pnl_pct REAL NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
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


# ------------------------------------------------- bb_bounce signal logic ---

def load_roadmap_events() -> dict:
    """User-maintained roadmap dates: {"SYMBOL or address": [{"name","date"}]}"""
    if not ROADMAP_PATH.exists():
        return {}
    try:
        return json.loads(ROADMAP_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def roadmap_event_within(symbol: str, address: str, days: int = ROADMAP_SIGNAL_DAYS) -> str | None:
    """Name of a roadmap event within the next `days`, if any."""
    events = load_roadmap_events()
    now = datetime.now(timezone.utc)
    for key in (symbol, symbol.upper(), address):
        for ev in events.get(key, []):
            try:
                dt = datetime.fromisoformat(ev["date"]).replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            delta_days = (dt - now).total_seconds() / 86400
            if 0 <= delta_days <= days:
                return ev.get("name", "roadmap event")
    return None


def detect_patterns(df: pd.DataFrame) -> list[str]:
    """Conservative bullish chart-pattern detectors on 15m candles."""
    patterns = []
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    if len(lows) >= 30:
        # Higher lows: minima of three consecutive 10-candle windows ascending.
        w1, w2, w3 = lows[-30:-20].min(), lows[-20:-10].min(), lows[-10:].min()
        if w1 < w2 < w3:
            patterns.append("higher_lows")
    if len(lows) >= 40:
        # Double bottom: two window minima within 2.5% of each other, at least
        # 10 candles apart, with price now recovering above both.
        first, second = lows[-40:-20].min(), lows[-20:].min()
        if abs(first - second) / max(first, second) <= 0.025 \
                and closes[-1] > max(first, second) * 1.03:
            patterns.append("double_bottom")
    return patterns


def check_bb_bounce_signal(df: pd.DataFrame,
                           has_roadmap: bool = False) -> tuple[bool, dict]:
    """The user's setup, on 15m candles:
      MANDATORY: a recent candle tagged the lower Bollinger Band and the
                 latest candle closed green.
      Confirmations (need all 3, or 2 when a roadmap event is near):
        - MACD histogram turning green (rising vs previous bar)
        - Stoch RSI %K bottomed (<25 within last 4 bars) and rising toward
          the midline (Stoch RSI saturates in 2-3 bars, so "currently low"
          would miss almost every real bounce at 15-min polling)
        - Momentum (MOM-10) trending up across the last 3 bars
    Returns (signal, gates_detail)."""
    if len(df) < 60:
        return False, {"reason": "insufficient candles"}
    close, low = df["close"], df["low"]

    _, _, bb_lower = compute_bollinger(close)
    macd_line, macd_signal, macd_hist = compute_macd(close)
    k, _d = compute_stoch_rsi(close)
    mom = close - close.shift(10)

    if bb_lower.iloc[-2:].isna().any() or pd.isna(k.iloc[-1]) or pd.isna(mom.iloc[-3]):
        return False, {"reason": "indicators not ready"}

    # Mandatory: lower-band tag within the last 2 candles + green latest candle.
    bb_touch = bool((low.iloc[-2] <= bb_lower.iloc[-2] * 1.005)
                    or (low.iloc[-1] <= bb_lower.iloc[-1] * 1.005))
    green_candle = bool(close.iloc[-1] > df["open"].iloc[-1])

    gates = {
        "bb_touch": bb_touch,
        "green_candle": green_candle,
        "macd_green": bool(macd_hist.iloc[-1] > macd_hist.iloc[-2]),
        "stoch_bottom_rising": bool(k.iloc[-4:].min() <= 25
                                    and k.iloc[-1] > k.iloc[-2]),
        "momentum_up": bool(mom.iloc[-1] > mom.iloc[-2]
                            and mom.iloc[-1] > mom.iloc[-3]),
    }
    confirmations = sum((gates["macd_green"], gates["stoch_bottom_rising"],
                         gates["momentum_up"]))
    needed = 2 if has_roadmap else 3
    signal = bb_touch and green_candle and confirmations >= needed
    gates["confirmations"] = f"{confirmations}/{needed}"
    return signal, gates


def fetch_candles_15m(client: httpx.Client, address: str,
                      hours: int = 30) -> pd.DataFrame | None:
    now = int(time.time())
    try:
        r = client.get(
            f"{BIRDEYE_BASE}/defi/ohlcv",
            headers=_headers(),
            params={"address": address, "type": "15m",
                    "time_from": now - hours * 3600, "time_to": now},
            timeout=30.0,
        )
        if r.status_code != 200:
            return None
        items = r.json().get("data", {}).get("items", [])
        if not items:
            return None
        df = pd.DataFrame([{
            "timestamp": it.get("unixTime"), "open": it.get("o"),
            "high": it.get("h"), "low": it.get("l"), "close": it.get("c"),
            "volume": it.get("v"),
        } for it in items]).dropna()
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception:
        return None


def review_transactions(client: httpx.Client, address: str) -> dict | None:
    """Are most recent swaps green (buys)? Returns flow stats or None."""
    try:
        r = client.get(
            f"{BIRDEYE_BASE}/defi/txs/token",
            headers=_headers(),
            params={"address": address, "offset": 0, "limit": 50,
                    "tx_type": "swap", "sort_type": "desc"},
            timeout=20.0,
        )
        if r.status_code != 200:
            return None
        items = r.json().get("data", {}).get("items", [])
    except Exception:
        return None
    if not items:
        return None

    buy_vol = sell_vol = 0.0
    buys = sells = 0
    for it in items:
        side = it.get("side")
        # USD value from the cash leg of the swap.
        usd = 0.0
        for leg in (it.get("quote") or {}, it.get("base") or {}):
            if leg.get("address") in CASH_MINTS and leg.get("price"):
                usd = abs(float(leg.get("uiChangeAmount") or 0)) * float(leg["price"])
                break
        if side == "buy":
            buys += 1
            buy_vol += usd
        elif side == "sell":
            sells += 1
            sell_vol += usd
    total_n, total_v = buys + sells, buy_vol + sell_vol
    if total_n == 0:
        return None
    return {
        "buy_count_pct": round(100 * buys / total_n, 1),
        "buy_volume_pct": round(100 * buy_vol / total_v, 1) if total_v else 0.0,
        "n_txs": total_n,
    }


def scan_bb_bounce(client: httpx.Client) -> list[dict]:
    """Bollinger-bounce candidates from the high-volume (trending) universe."""
    cfg = SNIPER_BB_BOUNCE
    try:
        r = client.get(
            f"{BIRDEYE_BASE}/defi/v3/token/list",
            headers=_headers(),
            params={"sort_by": "volume_1h_usd", "sort_type": "desc",
                    "min_liquidity": cfg["min_liquidity"],
                    "min_volume_1h_usd": cfg["min_volume_1h"], "limit": 50},
            timeout=30.0,
        )
        if r.status_code != 200:
            print(f"[bb_bounce] HTTP {r.status_code}", file=sys.stderr)
            return []
        items = r.json().get("data", {}).get("items", [])
    except Exception as e:
        print(f"[bb_bounce] {type(e).__name__}: {e}", file=sys.stderr)
        return []

    # Universe: trending tokens, not majors, currently in the dip/flat zone.
    universe = []
    for it in items:
        sym = (it.get("symbol") or "").upper()
        if sym in MAJOR_SYMBOLS or it.get("address") in CASH_MINTS:
            continue
        chg = it.get("price_change_1h_percent")
        if chg is None or chg > cfg["max_change_1h"]:
            continue
        universe.append(it)

    out = []
    charted = 0
    for it in universe[:cfg["candles_checked"]]:
        addr, sym = it.get("address", ""), it.get("symbol") or "?"
        df = fetch_candles_15m(client, addr)
        if df is None:
            continue
        charted += 1
        roadmap = roadmap_event_within(sym, addr)
        signal, gates = check_bb_bounce_signal(df, has_roadmap=bool(roadmap))
        if not signal:
            continue
        # Transaction review — only for tokens passing the indicator gates.
        flow = review_transactions(client, addr)
        if not flow or flow["buy_volume_pct"] < cfg["min_buy_volume_pct"]:
            continue
        meta = {
            "gates": gates,
            "flow": flow,
            "patterns": detect_patterns(df),
            "change_1h": round(it.get("price_change_1h_percent") or 0, 1),
        }
        if roadmap:
            meta["roadmap"] = roadmap
        out.append({
            "address": addr,
            "symbol": sym,
            "price": float(df["close"].iloc[-1]),
            "meta": meta,
        })
        time.sleep(0.2)
    print(f"[bb_bounce] {len(universe)} trending tokens in dip zone, "
          f"{charted} charts reviewed, {len(out)} signals")
    return out


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
        SELECT strategy, symbol, entry_price, shadow_size_usd, tokens_held,
               initial_out_usd
        FROM sniper_trades WHERE id = ?
    """, (trade_id,)).fetchone()
    strategy, sym, entry, size, tokens, initial_out = row
    # Total P&L includes any principal already taken out at 2x.
    pnl = (initial_out or 0) + tokens * price - size
    pnl_pct = (pnl / size * 100) if size else 0.0
    conn.execute("""
        UPDATE sniper_trades
        SET closed_at = ?, close_price = ?, close_reason = ?,
            pnl_usd = ?, pnl_pct = ?
        WHERE id = ?
    """, (int(time.time()), price, reason, pnl, pnl_pct, trade_id))
    return {"strategy": strategy, "symbol": sym, "pnl_usd": pnl,
            "pnl_pct": pnl_pct, "reason": reason}


def take_initial_out(conn: sqlite3.Connection, trade_id: int,
                     exit_price: float) -> dict:
    """Sell just enough tokens (at exit_price, slippage included) to recover
    the initial stake. Remaining tokens ride risk-free."""
    row = conn.execute("""
        SELECT strategy, symbol, shadow_size_usd, tokens_held
        FROM sniper_trades WHERE id = ?
    """, (trade_id,)).fetchone()
    strategy, sym, size, tokens = row
    tokens_sold = size / exit_price
    remaining = tokens - tokens_sold
    conn.execute("""
        UPDATE sniper_trades
        SET tokens_held = ?, took_initial_at = ?, initial_out_usd = ?
        WHERE id = ?
    """, (remaining, int(time.time()), size, trade_id))
    return {"strategy": strategy, "symbol": sym, "recovered_usd": size,
            "runner_tokens": remaining, "price": exit_price}


def mark_open_to_market(conn: sqlite3.Connection, prices: dict[str, float],
                        now: int) -> tuple[list[dict], list[dict]]:
    """Apply the exit ladder to all open shadows. Returns (derisks, closes).

    Ladder, in order:
      1. Value >= 2x stake and initial not yet out -> sell back the stake,
         remainder rides risk-free.
      2. Initial not out and P&L <= -stop_pct -> STOP_LOSS.
      3. Initial not out, position peaked above the break-even trigger and
         fell back to flat -> BREAK_EVEN_STOP (a winner never becomes a loser).
      4. Past max hold -> MAX_HOLD.
    """
    derisks: list[dict] = []
    closes: list[dict] = []
    open_rows = conn.execute("""
        SELECT id, strategy, address, opened_at, shadow_size_usd, tokens_held,
               took_initial_at, initial_out_usd, peak_pnl_pct
        FROM sniper_trades WHERE closed_at IS NULL
    """).fetchall()
    for (trade_id, strategy, addr, opened_at, size, tokens,
         took_initial, initial_out, peak) in open_rows:
        market = prices.get(addr)
        if not market:
            continue
        cfg = STRATEGIES[strategy]
        exit_price = market * (1 - SLIP)
        pnl_pct = ((initial_out or 0) + tokens * exit_price - size) / size * 100

        if pnl_pct > (peak or 0):
            conn.execute("UPDATE sniper_trades SET peak_pnl_pct = ? WHERE id = ?",
                         (pnl_pct, trade_id))
            peak = pnl_pct

        if not took_initial and tokens * exit_price >= size * SHADOW_TAKE_INITIAL_MULT:
            derisks.append(take_initial_out(conn, trade_id, exit_price))
            continue
        if not took_initial and pnl_pct <= -cfg["stop_pct"]:
            closes.append(close_shadow(conn, trade_id, exit_price, "STOP_LOSS"))
        elif (not took_initial and peak >= SHADOW_BREAK_EVEN_TRIGGER_PCT
                and pnl_pct <= 0):
            closes.append(close_shadow(conn, trade_id, exit_price, "BREAK_EVEN_STOP"))
        elif opened_at < now - cfg["max_hold_hours"] * 3600:
            closes.append(close_shadow(conn, trade_id, exit_price, "MAX_HOLD"))
    return derisks, closes


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
        bounces = scan_bb_bounce(client)

        candidates = {
            "fresh_listing": fresh,
            "breakout": breakouts,
            "bb_bounce": bounces,
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
                max_score = (SAFETY_MAX_SCORE_FRESH if strategy == "fresh_listing"
                             else SAFETY_MAX_SCORE)
                safe, safety = is_safe_to_buy(
                    client, c["address"], c["symbol"], max_score=max_score)
                if not safe:
                    reason = ("no security data" if safety is None
                              else "; ".join(safety["flags"]) or
                              f"score {safety['score']:.0f}")
                    print(f"  [safety] BLOCKED {c['symbol']} ({strategy}): {reason}")
                    continue
                price = c.get("price") or fetch_price(client, c["address"])
                if not price:
                    continue
                c["meta"]["safety_score"] = safety["score"]
                if safety["flags"]:
                    c["meta"]["safety_flags"] = safety["flags"]
                opens.append(open_shadow(
                    conn, strategy, c["address"], c["symbol"], price, c["meta"]))
                opened_this_cycle += 1
            conn.commit()

        # --- mark open shadows to market ---
        addrs = [r[0] for r in conn.execute(
            "SELECT DISTINCT address FROM sniper_trades WHERE closed_at IS NULL"
        ).fetchall()]
        prices = fetch_prices(client, addrs)
        derisks, mtm_closes = mark_open_to_market(conn, prices, now)
        closes.extend(mtm_closes)
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
    return {"opens": opens, "closes": closes, "derisks": derisks,
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
        n_open, n_free = conn.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN took_initial_at IS NOT NULL THEN 1 ELSE 0 END)
            FROM sniper_trades
            WHERE strategy = ? AND closed_at IS NULL
        """, (strategy,)).fetchone()
        open_txt = f"{n_open} open"
        if n_free:
            open_txt += f" ({n_free} risk-free)"
        lines.append(f"  {strategy}: {n} closed, {wr:.0f}% WR, "
                     f"{r['total']:+.2f} (avg {r['avg_pct']:+.1f}%), {open_txt}")

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
    for d in summary.get("derisks", []):
        telegram_notifier.send(
            f"<b>SNIPER — INITIAL OUT (2x)</b>\n"
            f"<b>{d['symbol']}</b> ({d['strategy']})\n"
            f"Recovered ${d['recovered_usd']:.2f} stake @ {d['price']:.10f}\n"
            f"Remainder rides risk-free."
        )
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
    print(f"Opens: {len(summary['opens'])}  Closes: {len(summary['closes'])}  "
          f"Initial-outs: {len(summary['derisks'])}")
    for o in summary["opens"]:
        print(f"  OPEN  [{o['strategy']}] {o['symbol']} @ {o['price']:.10f}  {o['meta']}")
    for d in summary["derisks"]:
        print(f"  2X    [{d['strategy']}] {d['symbol']} initial ${d['recovered_usd']:.2f} "
              f"out, runner rides free")
    for c in summary["closes"]:
        print(f"  CLOSE [{c['strategy']}] {c['symbol']} {c['pnl_usd']:+.2f} ({c['reason']})")
    p = summary["pulse"]
    if p["median_1h"] is not None:
        print(f"Pulse: top-100 median 1h {p['median_1h']:+.1f}%, "
              f"{p['pct_gainers']:.0f}% gainers, {p['fresh_seen']} fresh listings")
    notify(summary)


if __name__ == "__main__":
    main()
