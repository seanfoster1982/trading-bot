from pathlib import Path

content = '''"""Backtester - replays the LIVE strategy against historical candles.

Key design: instead of re-implementing the strategy (which caused divergence
bugs on the first attempt), this imports strategy.py and monkeypatches its two
data-access functions (get_latest_indicators, get_previous_indicators) to read
point-in-time data filtered by a simulated clock. Every downstream strategy
function then operates on historical data automatically.

This guarantees the backtest runs the SAME logic as the live bot.

Usage:
  python scripts/backtest.py --token TROLL --days 20      # single token
  python scripts/backtest.py --all --days 20              # all tokens
'''
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

DB_PATH = Path("data/memecoins.db")

# Simulated clock - the strategy data functions read data <= this timestamp
_SIM_NOW = {"ts": 0}


def set_sim_now(ts: int):
    _SIM_NOW["ts"] = ts


def pit_get_latest_indicators(address: str, interval: str):
    """Point-in-time replacement for strategy.get_latest_indicators.
    Returns the most recent indicator row at or before the simulated clock."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT close, macd_line, macd_signal, macd_hist, stoch_rsi_k, stoch_rsi_d,
               bb_upper, bb_middle, bb_lower, ema_50, ema_200, atr_14,
               volume_sma_20, obv, vwap, timestamp
        FROM indicators
        WHERE address=? AND interval=? AND timestamp <= ?
        ORDER BY timestamp DESC LIMIT 1
    """, (address, interval, _SIM_NOW["ts"])).fetchone()
    conn.close()
    if not row:
        return None
    keys = ["close","macd_line","macd_signal","macd_hist","stoch_rsi_k","stoch_rsi_d",
            "bb_upper","bb_middle","bb_lower","ema_50","ema_200","atr_14",
            "volume_sma_20","obv","vwap","timestamp"]
    return dict(zip(keys, row))


def pit_get_previous_indicators(address: str, interval: str, n_back: int = 1):
    """Point-in-time replacement for strategy.get_previous_indicators."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT close, macd_line, macd_signal, macd_hist, stoch_rsi_k, stoch_rsi_d,
               bb_upper, bb_middle, bb_lower, ema_50, ema_200, atr_14,
               volume_sma_20, obv, vwap, timestamp
        FROM indicators
        WHERE address=? AND interval=? AND timestamp <= ?
        ORDER BY timestamp DESC LIMIT ?
    """, (address, interval, _SIM_NOW["ts"], n_back + 1)).fetchall()
    conn.close()
    if len(rows) <= n_back:
        return None
    row = rows[n_back]
    keys = ["close","macd_line","macd_signal","macd_hist","stoch_rsi_k","stoch_rsi_d",
            "bb_upper","bb_middle","bb_lower","ema_50","ema_200","atr_14",
            "volume_sma_20","obv","vwap","timestamp"]
    return dict(zip(keys, row))


def load_5m_candles(address: str, since_ts: int):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT timestamp, open, high, low, close
        FROM candles WHERE address=? AND interval='5m' AND timestamp>=?
        ORDER BY timestamp ASC
    """, (address, since_ts)).fetchall()
    conn.close()
    return rows


@dataclass
class BTTrade:
    symbol: str
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    pnl_pct: float
    reason: str


def simulate_token(symbol: str, address: str, days: int, strat_mod) -> list[BTTrade]:
    """Walk the 5m candles for one token, fire the live strategy at each step."""
    since_ts = int(time.time()) - days * 86400
    candles = load_5m_candles(address, since_ts)
    if len(candles) < 250:
        return []

    trades: list[BTTrade] = []
    in_position = False
    entry_price = stop = target = 0.0
    entry_ts = 0
    last_stop_exit_ts = 0  # for cooldown simulation

    ATR_STOP_MULT = getattr(strat_mod, "ATR_STOP_MULTIPLIER", 2.0)

    for idx in range(200, len(candles)):
        ts, o, h, l, c = candles[idx]
        set_sim_now(ts)

        if in_position:
            # check stop / target against this candle's low/high
            if l <= stop:
                pnl = (stop - entry_price) / entry_price
                trades.append(BTTrade(symbol, entry_ts, entry_price, ts, stop, pnl, "STOP"))
                in_position = False
                last_stop_exit_ts = ts
            elif target and h >= target:
                pnl = (target - entry_price) / entry_price
                trades.append(BTTrade(symbol, entry_ts, entry_price, ts, target, pnl, "TARGET"))
                in_position = False
            continue

        # cooldown: skip if within 24h of a stop exit
        if last_stop_exit_ts and (ts - last_stop_exit_ts) < 24 * 3600:
            continue

        # Drive the LIVE strategy logic via monkeypatched data functions
        aligned, _ = strat_mod.check_higher_tf_alignment(address)
        if not aligned:
            continue
        trig, _, _ = strat_mod.detect_15m_entry(address)
        if not trig:
            continue
        conf, _ = strat_mod.confirm_5m(address)
        if not conf:
            continue

        # Entry confirmed - compute stop/target like the live strategy
        ind = pit_get_latest_indicators(address, "5m")
        atr = ind.get("atr_14") if ind else None
        entry_price = c
        if atr and atr > 0:
            stop = entry_price - (atr * ATR_STOP_MULT)
        else:
            stop = entry_price * 0.95
        risk = entry_price - stop
        target = entry_price + 2 * risk  # 2R target
        entry_ts = ts
        in_position = True

    return trades


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", type=str, default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--days", type=int, default=20)
    args = parser.parse_args()

    import strategy as strat_mod
    # Monkeypatch the data-access functions to be point-in-time
    strat_mod.get_latest_indicators = pit_get_latest_indicators
    strat_mod.get_previous_indicators = pit_get_previous_indicators

    conn = sqlite3.connect(DB_PATH)
    if args.token:
        row = conn.execute(
            "SELECT DISTINCT symbol, address FROM screened_tokens WHERE symbol=? LIMIT 1",
            (args.token,)).fetchone()
        targets = [row] if row else []
    else:
        targets = conn.execute("""
            SELECT DISTINCT s.symbol, c.address
            FROM candles c
            JOIN screened_tokens s ON s.address = c.address
            WHERE c.interval='5m'
            GROUP BY c.address HAVING COUNT(*) >= 500
        """).fetchall()
    conn.close()

    print(f"Backtesting {len(targets)} token(s) over {args.days} days\\n")

    all_trades = []
    for sym, addr in targets:
        trades = simulate_token(sym, addr, args.days, strat_mod)
        all_trades.extend(trades)
        if args.token:  # verbose for single token
            for t in trades:
                print(f"  {t.symbol} entry={t.entry_price:.8f} exit={t.exit_price:.8f} "
                      f"{t.pnl_pct*100:+.1f}% {t.reason}")

    if not all_trades:
        print("No trades generated.")
        return

    wins = [t for t in all_trades if t.pnl_pct > 0]
    losses = [t for t in all_trades if t.pnl_pct <= 0]
    total_pct = sum(t.pnl_pct for t in all_trades)
    wr = len(wins) / len(all_trades) * 100

    print(f"\\n=== BACKTEST RESULTS ({args.days} days) ===")
    print(f"  Total trades:  {len(all_trades)}")
    print(f"  Wins/Losses:   {len(wins)} / {len(losses)}")
    print(f"  Win rate:      {wr:.1f}%")
    if wins:
        print(f"  Avg win:       {sum(t.pnl_pct for t in wins)/len(wins)*100:+.1f}%")
    if losses:
        print(f"  Avg loss:      {sum(t.pnl_pct for t in losses)/len(losses)*100:+.1f}%")
    print(f"  Sum of returns: {total_pct*100:+.1f}% (before position sizing)")
    print(f"  Expectancy:    {total_pct/len(all_trades)*100:+.2f}% per trade")


if __name__ == "__main__":
    main()
'''

Path("scripts/backtest.py").write_text(content, encoding="utf-8")
print("Wrote scripts/backtest.py")

import ast
try:
    ast.parse(content)
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
