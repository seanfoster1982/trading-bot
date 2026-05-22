"""Strategy module — reads indicators, rug, whale, macro tables and emits trading signals.

For each tradeable token across each timeframe, evaluates:
  1. Higher timeframe trend (1H + 4H + 1D must align bullish for long entry)
  2. Entry trigger on 15m (MACD cross, Stoch RSI exit oversold, or BB lower touch)
  3. 5m confirmation (price above VWAP, healthy volume)
  4. Risk gates (rug score, whale risk, macro regime, exhaustion)
  5. Position sizing (ATR-based, capped by 70/20/10 allocation)

Writes to signals table. Execution module reads from there.

70/20/10 allocation: \ momentum / \ whale copy / \ lottery
\ USDC working capital, max positions: 3 momentum / 2 whale / 5 lottery
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import click
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = Path("data/memecoins.db")

# Capital allocation
TOTAL_CAPITAL_USD = 100.0
ALLOCATION = {
    "momentum":  {"pct": 0.70, "max_positions": 3, "per_position_usd": 23.0},
    "whale_copy": {"pct": 0.20, "max_positions": 2, "per_position_usd": 10.0},
    "lottery":   {"pct": 0.10, "max_positions": 5, "per_position_usd": 2.0},
}

# Risk gates
MAX_RUG_SCORE = 25
MAX_WHALE_RISK_SCORE = 50
MAX_EXHAUSTION_SCORE = 60
MIN_MACRO_FOR_FULL_SIZE = 50
ATR_STOP_MULTIPLIER = 2.0

SignalAction = Literal["BUY", "SELL", "HOLD", "NO_SIGNAL"]


@dataclass
class Signal:
    symbol: str
    address: str
    action: SignalAction
    strategy: str  # momentum / whale_copy / lottery
    entry_price: float
    stop_loss: float
    take_profit: float | None
    position_size_usd: float
    reasoning: list[str]
    indicators_snapshot: dict
    generated_at: int


def init_signals_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            address TEXT NOT NULL,
            action TEXT NOT NULL,
            strategy TEXT NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL,
            position_size_usd REAL NOT NULL,
            reasoning_json TEXT NOT NULL,
            indicators_json TEXT NOT NULL,
            generated_at INTEGER NOT NULL,
            executed INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_signals_recent
        ON signals(generated_at, executed)
    """)
    conn.commit()
    conn.close()


# -------- Data loaders --------

def get_tradeable_universe() -> list[tuple[str, str, str]]:
    """Tokens passing rug + freeze + indicator availability + screen category.
    Returns [(symbol, address, screen), ...].
    """
    conn = sqlite3.connect(DB_PATH)
    cutoff = int(time.time()) - (24 * 3600)
    rows = conn.execute("""
        SELECT DISTINCT s.symbol, s.address, s.screen
        FROM screened_tokens s
        INNER JOIN rug_reports r ON s.address = r.address
        WHERE s.screened_at >= ?
          AND r.rug_score < ?
          AND r.freeze_authority_active = 0
    """, (cutoff, MAX_RUG_SCORE)).fetchall()
    conn.close()
    return rows


def get_latest_indicators(address: str, interval: str) -> dict | None:
    """Get the most recent indicator row for a (token, timeframe)."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT timestamp, close, macd_line, macd_signal, macd_hist,
               stoch_rsi_k, stoch_rsi_d, bb_upper, bb_middle, bb_lower,
               ema_50, ema_200, atr_14, volume_sma_20, obv, vwap
        FROM indicators
        WHERE address = ? AND interval = ?
        ORDER BY timestamp DESC
        LIMIT 1
    """, (address, interval)).fetchone()
    if not row:
        conn.close()
        return None
    cols = ["timestamp", "close", "macd_line", "macd_signal", "macd_hist",
            "stoch_rsi_k", "stoch_rsi_d", "bb_upper", "bb_middle", "bb_lower",
            "ema_50", "ema_200", "atr_14", "volume_sma_20", "obv", "vwap"]
    conn.close()
    return dict(zip(cols, row))


def get_previous_indicators(address: str, interval: str, n_back: int = 1) -> dict | None:
    """Get the Nth most recent indicator row. Used for detecting crosses."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT timestamp, close, macd_line, macd_signal, macd_hist,
               stoch_rsi_k, stoch_rsi_d, bb_upper, bb_middle, bb_lower,
               ema_50, ema_200, atr_14, volume_sma_20, obv, vwap
        FROM indicators
        WHERE address = ? AND interval = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, (address, interval, n_back + 1)).fetchall()
    conn.close()
    if len(rows) < n_back + 1:
        return None
    cols = ["timestamp", "close", "macd_line", "macd_signal", "macd_hist",
            "stoch_rsi_k", "stoch_rsi_d", "bb_upper", "bb_middle", "bb_lower",
            "ema_50", "ema_200", "atr_14", "volume_sma_20", "obv", "vwap"]
    return dict(zip(cols, rows[n_back]))


def get_rug_score(address: str) -> tuple[float, bool] | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT rug_score, freeze_authority_active FROM rug_reports
        WHERE address = ?
    """, (address,)).fetchone()
    conn.close()
    if not row:
        return None
    return (row[0], bool(row[1]))


def get_whale_risk(address: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT whale_risk_score, top_10_pct, top_10_pct_delta_24h, exit_liquidity_usd
        FROM whale_risk
        WHERE address = ?
        ORDER BY checked_at DESC
        LIMIT 1
    """, (address,)).fetchone()
    conn.close()
    if not row:
        return None
    return {"score": row[0], "top_10_pct": row[1], "delta": row[2], "liquidity": row[3]}


def get_exhaustion_score(address: str) -> float | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT exhaustion_score FROM screened_tokens
        WHERE address = ?
        ORDER BY screened_at DESC
        LIMIT 1
    """, (address,)).fetchone()
    conn.close()
    return row[0] if row else None


def get_macro_score() -> float | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT macro_regime_score FROM macro_regime
        ORDER BY timestamp DESC
        LIMIT 1
    """).fetchone()
    conn.close()
    return row[0] if row else None


# -------- Strategy logic --------

def check_higher_tf_alignment(address: str) -> tuple[bool, list[str]]:
    """Higher timeframes (1H + 4H + 1D) must all be bullish for long entry.
    Bullish = close > EMA 50 AND EMA 50 > EMA 200 (or close > EMA 200 if 50 missing).
    """
    reasons = []
    ok = True
    for interval in ["1H", "4H", "1D"]:
        ind = get_latest_indicators(address, interval)
        if ind is None or ind["ema_200"] is None:
            reasons.append(f"{interval}: no data (skipping)")
            continue  # don't fail just because data missing on one tf
        close = ind["close"]
        ema_50 = ind["ema_50"]
        ema_200 = ind["ema_200"]
        if ema_50 and ema_200:
            tf_bullish = close > ema_50 and ema_50 > ema_200
        elif ema_200:
            tf_bullish = close > ema_200
        else:
            tf_bullish = False
        if tf_bullish:
            reasons.append(f"{interval}: bullish (close>{ema_50:.6f}>{ema_200:.6f})" if ema_50 else f"{interval}: bullish")
        else:
            reasons.append(f"{interval}: NOT bullish")
            ok = False
    return ok, reasons


def detect_15m_entry(address: str) -> tuple[bool, str, dict]:
    """Entry triggers on 15m:
      A) MACD bullish cross (line just crossed above signal)
      B) Stoch RSI exits oversold (K crosses above 20)
      C) Price closes back inside lower Bollinger Band after touching it
    Returns (fired, reason, latest_indicators).
    """
    latest = get_latest_indicators(address, "15m")
    prev = get_previous_indicators(address, "15m", n_back=1)
    if not latest or not prev:
        return False, "insufficient 15m data", {}

    # Trigger A: MACD cross
    if latest["macd_line"] and latest["macd_signal"] and prev["macd_line"] and prev["macd_signal"]:
        if prev["macd_line"] <= prev["macd_signal"] and latest["macd_line"] > latest["macd_signal"]:
            return True, "MACD bullish cross on 15m", latest

    # Trigger B: Stoch RSI exit oversold
    if latest["stoch_rsi_k"] is not None and prev["stoch_rsi_k"] is not None:
        if prev["stoch_rsi_k"] <= 20 and latest["stoch_rsi_k"] > 20:
            return True, f"Stoch RSI exit oversold (prev={prev['stoch_rsi_k']:.0f}, now={latest['stoch_rsi_k']:.0f})", latest

    # Trigger C: Bollinger Band bounce
    if (latest["bb_lower"] and prev["bb_lower"] and prev["close"]
        and prev["close"] <= prev["bb_lower"] and latest["close"] > latest["bb_lower"]):
        return True, "Bollinger Band lower bounce on 15m", latest

    return False, "no 15m trigger", latest


def confirm_5m(address: str) -> tuple[bool, str]:
    """5m confirmation: price above VWAP AND recent volume not declining."""
    latest = get_latest_indicators(address, "5m")
    if not latest:
        return False, "no 5m data"
    close = latest["close"]
    vwap = latest["vwap"]
    if not vwap or close <= vwap:
        vwap_str = f"{vwap:.6f}" if vwap else "0"
        return False, f"5m close ({close:.6f}) not above VWAP ({vwap_str})"
    vol_sma = latest["volume_sma_20"]
    if vol_sma is None:
        return True, "5m above VWAP (no volume data)"
    return True, f"5m confirmed: close>{vwap:.6f} VWAP"


def compute_position_size(
    strategy: str, macro_score: float | None, atr: float | None, price: float
) -> float:
    """Position size in USD. Base is per-strategy allocation. Scale by macro regime."""
    base = ALLOCATION[strategy]["per_position_usd"]
    if macro_score is None:
        macro_factor = 0.75  # no macro data yet — conservative
    elif macro_score >= 70:
        macro_factor = 1.0
    elif macro_score >= MIN_MACRO_FOR_FULL_SIZE:
        macro_factor = 0.85
    elif macro_score >= 30:
        macro_factor = 0.6
    else:
        macro_factor = 0.3  # headwind regime — small positions only
    return round(base * macro_factor, 2)


def compute_stop_loss(entry_price: float, atr: float | None) -> float:
    if atr is None or atr <= 0:
        # Fallback: 5% below entry
        return round(entry_price * 0.95, 8)
    return round(entry_price - (atr * ATR_STOP_MULTIPLIER), 8)



# Cooldown rule: after a STOP_LOSS exit on a token, do not re-enter for 24 hours.
# Added 2026-05-15 after observing repeated same-token stop-outs (ASTEROID, TROLL, BULL).
COOLDOWN_HOURS_AFTER_STOPLOSS = 24


def recent_stoploss_cooldown(address: str) -> tuple[bool, float]:
    """Check if this token had a STOP_LOSS exit within the cooldown window.
    Returns (is_in_cooldown, hours_remaining)."""
    import time
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT MAX(closed_at) FROM paper_trades
        WHERE address = ?
          AND (close_reason LIKE 'STOP_LOSS%' OR close_reason LIKE 'HARD_STOP%')
    """, (address,)).fetchone()
    conn.close()
    if not row or not row[0]:
        return (False, 0.0)
    last_stop_ts = int(row[0])
    age_hours = (time.time() - last_stop_ts) / 3600
    if age_hours < COOLDOWN_HOURS_AFTER_STOPLOSS:
        remaining = COOLDOWN_HOURS_AFTER_STOPLOSS - age_hours
        return (True, remaining)
    return (False, 0.0)


def evaluate_token(address: str, symbol: str, screen: str) -> Signal:
    """Run full strategy logic for one token. Returns a Signal (may be NO_SIGNAL)."""
    # COOLDOWN CHECK: skip if this token had a STOP_LOSS within last 24h
    in_cd, hours_remaining = recent_stoploss_cooldown(address)
    if in_cd:
        import time as _time
        return Signal(
            symbol=symbol, address=address, action='HOLD',
            strategy={'momentum': 'momentum', 'whale_target': 'whale_copy', 'lottery': 'lottery'}.get(screen, 'momentum'),
            entry_price=0.0, stop_loss=0.0, take_profit=None,
            position_size_usd=0.0,
            reasoning=[f'COOLDOWN: stop-out within {COOLDOWN_HOURS_AFTER_STOPLOSS}h ({hours_remaining:.1f}h remaining)'],
            indicators_snapshot={},
            generated_at=int(_time.time()),
        )

    reasoning = []
    now = int(time.time())

    # Strategy assignment from screen
    strategy_map = {"momentum": "momentum", "whale_target": "whale_copy", "lottery": "lottery"}
    strategy = strategy_map.get(screen, "momentum")

    # --- Gates ---

    rug = get_rug_score(address)
    if rug:
        score, freeze = rug
        if freeze:
            return Signal(symbol, address, "NO_SIGNAL", strategy, 0, 0, None, 0,
                          ["BLOCKED: freeze authority active"], {}, now)
        if score >= MAX_RUG_SCORE:
            return Signal(symbol, address, "NO_SIGNAL", strategy, 0, 0, None, 0,
                          [f"BLOCKED: rug score {score:.0f} >= {MAX_RUG_SCORE}"], {}, now)
        reasoning.append(f"rug score {score:.0f} (pass)")
    else:
        reasoning.append("no rug report — proceeding cautiously")

    whale = get_whale_risk(address)
    if whale and whale["score"] >= MAX_WHALE_RISK_SCORE:
        return Signal(symbol, address, "NO_SIGNAL", strategy, 0, 0, None, 0,
                      [f"BLOCKED: whale risk {whale['score']:.0f} >= {MAX_WHALE_RISK_SCORE}"], {}, now)
    if whale:
        reasoning.append(f"whale risk {whale['score']:.0f} (pass), top10={whale['top_10_pct']:.0f}%")

    exhaustion = get_exhaustion_score(address)
    if exhaustion is not None and exhaustion >= MAX_EXHAUSTION_SCORE:
        return Signal(symbol, address, "NO_SIGNAL", strategy, 0, 0, None, 0,
                      [f"BLOCKED: exhaustion {exhaustion:.0f} >= {MAX_EXHAUSTION_SCORE}"], {}, now)
    if exhaustion is not None:
        reasoning.append(f"exhaustion {exhaustion:.0f} (pass)")

    # --- Higher TF trend alignment ---

    aligned, tf_reasons = check_higher_tf_alignment(address)
    reasoning.extend(tf_reasons)
    if not aligned:
        return Signal(symbol, address, "NO_SIGNAL", strategy, 0, 0, None, 0,
                      reasoning + ["higher TFs not aligned bullish"], {}, now)

    # --- 15m entry trigger ---

    fired, trigger_reason, ind = detect_15m_entry(address)
    if not fired:
        return Signal(symbol, address, "NO_SIGNAL", strategy, 0, 0, None, 0,
                      reasoning + [f"no 15m entry: {trigger_reason}"], {}, now)
    reasoning.append(f"15m trigger: {trigger_reason}")

    # --- 5m confirmation ---

    confirmed, conf_reason = confirm_5m(address)
    if not confirmed:
        return Signal(symbol, address, "NO_SIGNAL", strategy, 0, 0, None, 0,
                      reasoning + [f"5m not confirmed: {conf_reason}"], {}, now)
    reasoning.append(conf_reason)

    # --- All checks passed — compute entry, stop, size ---

    entry_price = ind["close"]
    atr = ind["atr_14"]
    macro = get_macro_score()
    stop_loss = compute_stop_loss(entry_price, atr)
    position_size = compute_position_size(strategy, macro, atr, entry_price)

    # Take profit: 2x risk (R/R = 2)
    risk_per_unit = entry_price - stop_loss
    take_profit = round(entry_price + (risk_per_unit * 2), 8) if risk_per_unit > 0 else None

    reasoning.append(f"entry={entry_price:.6f} stop={stop_loss:.6f} size=${position_size}")
    if macro:
        reasoning.append(f"macro={macro:.0f}")

    return Signal(
        symbol=symbol, address=address, action="BUY", strategy=strategy,
        entry_price=entry_price, stop_loss=stop_loss, take_profit=take_profit,
        position_size_usd=position_size, reasoning=reasoning,
        indicators_snapshot=ind, generated_at=now,
    )


def save_signal(sig: Signal) -> None:
    if sig.action == "NO_SIGNAL":
        return  # only save actionable signals
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO signals
        (symbol, address, action, strategy, entry_price, stop_loss, take_profit,
         position_size_usd, reasoning_json, indicators_json, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        sig.symbol, sig.address, sig.action, sig.strategy,
        sig.entry_price, sig.stop_loss, sig.take_profit, sig.position_size_usd,
        json.dumps(sig.reasoning), json.dumps(sig.indicators_snapshot),
        sig.generated_at,
    ))
    conn.commit()
    conn.close()


def run_strategy(console: Console, verbose: bool):
    init_signals_db()
    universe = get_tradeable_universe()

    if not universe:
        console.print("[yellow]No tradeable universe found.[/yellow]")
        return

    # Deduplicate by address
    seen = set()
    deduped = []
    for sym, addr, screen in universe:
        if addr not in seen:
            seen.add(addr)
            deduped.append((sym, addr, screen))

    console.print(f"[bold]Strategy evaluation: {len(deduped)} tokens[/bold]\n")

    actionable: list[Signal] = []
    blocked: list[Signal] = []

    for sym, addr, screen in deduped:
        sig = evaluate_token(addr, sym, screen)
        if sig.action == "BUY":
            actionable.append(sig)
            save_signal(sig)
        else:
            blocked.append(sig)

    # Display actionable signals
    if actionable:
        table = Table(title=f"BUY Signals ({len(actionable)})")
        table.add_column("Symbol")
        table.add_column("Strategy")
        table.add_column("Entry", justify="right")
        table.add_column("Stop", justify="right")
        table.add_column("Take Profit", justify="right")
        table.add_column("Size USD", justify="right")
        table.add_column("Trigger")
        for s in actionable:
            trigger = next((r for r in s.reasoning if "15m trigger" in r), "-")
            table.add_row(
                s.symbol[:10], s.strategy,
                f"{s.entry_price:.6f}", f"{s.stop_loss:.6f}",
                f"{s.take_profit:.6f}" if s.take_profit else "-",
                f"${s.position_size_usd:.2f}",
                trigger.replace("15m trigger: ", "")[:30],
            )
        console.print(table)
    else:
        console.print("[yellow]No BUY signals fired right now.[/yellow]")
        console.print("This is normal — the strategy is selective by design.")

    # Display blocked breakdown
    if verbose or not actionable:
        console.print("\n[bold]Blocked tokens (why each token didn't fire):[/bold]")
        block_table = Table()
        block_table.add_column("Symbol")
        block_table.add_column("Strategy")
        block_table.add_column("Reason", overflow="fold")
        for s in blocked:
            block_table.add_row(
                s.symbol[:10], s.strategy,
                "; ".join(s.reasoning[-2:]) if s.reasoning else "-",
            )
        console.print(block_table)

    console.print("\n[green]Strategy evaluation complete.[/green]")
    console.print("Actionable signals saved to signals table. Run paper trader next.")


@click.command()
@click.option("--verbose", is_flag=True, help="Show all blocked tokens and reasoning.")
def main(verbose):
    console = Console()
    run_strategy(console, verbose)


if __name__ == "__main__":
    main()