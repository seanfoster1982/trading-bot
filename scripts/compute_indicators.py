"""Indicator computer — calculates 6 technical indicators for every token-timeframe
combination in the candles table. Results stored in indicators table for the
strategy module to read.

Indicators per candle:
  - MACD line, MACD signal, MACD histogram
  - Stochastic RSI K%, D%
  - Bollinger Bands upper, middle, lower
  - EMA 50, EMA 200
  - ATR (14)
  - Volume SMA (20), OBV, VWAP
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import click
import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale_config import WHALE_ONLY, ACTIVE_SCREEN  # noqa: E402

DB_PATH = Path("data/memecoins.db")

# Minimum candles required to compute indicators reliably
# (we need at least 200 for EMA 200 to mean anything)
MIN_CANDLES = 200

# Only compute for tokens screened within this window (matches ingest/strategy).
SCREEN_LOOKBACK_SEC = 24 * 3600


def init_indicators_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS indicators (
            address TEXT NOT NULL,
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            close REAL NOT NULL,
            macd_line REAL,
            macd_signal REAL,
            macd_hist REAL,
            stoch_rsi_k REAL,
            stoch_rsi_d REAL,
            bb_upper REAL,
            bb_middle REAL,
            bb_lower REAL,
            ema_50 REAL,
            ema_200 REAL,
            atr_14 REAL,
            volume_sma_20 REAL,
            obv REAL,
            vwap REAL,
            PRIMARY KEY (address, interval, timestamp)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_indicators_lookup
        ON indicators(address, interval, timestamp)
    """)
    conn.commit()
    conn.close()


def load_candles(address: str, interval: str) -> pd.DataFrame:
    """Load all candles for a (token, interval), ordered by time ascending."""
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT timestamp, open, high, low, close, volume
        FROM candles
        WHERE address = ? AND interval = ?
        ORDER BY timestamp ASC
        """,
        conn, params=(address, interval),
    )
    conn.close()
    return df


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line, histogram. Standard 12/26/9."""
    ema_12 = ema(close, 12)
    ema_26 = ema(close, 26)
    macd_line = ema_12 - ema_26
    signal_line = ema(macd_line, 9)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_stoch_rsi(
    close: pd.Series, rsi_period: int = 14, stoch_period: int = 14,
    k_smooth: int = 3, d_smooth: int = 3,
) -> tuple[pd.Series, pd.Series]:
    """Stochastic RSI: %K and %D lines."""
    rsi_vals = rsi(close, rsi_period)
    rsi_min = rsi_vals.rolling(window=stoch_period, min_periods=stoch_period).min()
    rsi_max = rsi_vals.rolling(window=stoch_period, min_periods=stoch_period).max()
    denom = (rsi_max - rsi_min).replace(0, np.nan)
    stoch_rsi = ((rsi_vals - rsi_min) / denom) * 100
    k = stoch_rsi.rolling(window=k_smooth, min_periods=k_smooth).mean()
    d = k.rolling(window=d_smooth, min_periods=d_smooth).mean()
    return k, d


def compute_bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands: upper, middle (SMA), lower."""
    middle = close.rolling(window=period, min_periods=period).mean()
    std = close.rolling(window=period, min_periods=period).std()
    upper = middle + (num_std * std)
    lower = middle - (num_std * std)
    return upper, middle, lower


def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range — volatility measure used for position sizing."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = true_range.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    return atr


def compute_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume: cumulative volume weighted by price direction."""
    direction = close.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    obv = (direction * volume).cumsum()
    return obv


def compute_vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Volume-Weighted Average Price (cumulative)."""
    typical = (high + low + close) / 3
    cum_tp_vol = (typical * volume).cumsum()
    cum_vol = volume.cumsum().replace(0, np.nan)
    return cum_tp_vol / cum_vol


def compute_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Given OHLCV dataframe (timestamp, open, high, low, close, volume),
    return dataframe with all indicators added as columns.
    """
    out = df.copy()
    macd_line, macd_signal, macd_hist = compute_macd(out["close"])
    out["macd_line"] = macd_line
    out["macd_signal"] = macd_signal
    out["macd_hist"] = macd_hist

    k, d = compute_stoch_rsi(out["close"])
    out["stoch_rsi_k"] = k
    out["stoch_rsi_d"] = d

    upper, middle, lower = compute_bollinger(out["close"])
    out["bb_upper"] = upper
    out["bb_middle"] = middle
    out["bb_lower"] = lower

    out["ema_50"] = ema(out["close"], 50)
    out["ema_200"] = ema(out["close"], 200)
    out["atr_14"] = compute_atr(out["high"], out["low"], out["close"])
    out["volume_sma_20"] = out["volume"].rolling(window=20, min_periods=20).mean()
    out["obv"] = compute_obv(out["close"], out["volume"])
    out["vwap"] = compute_vwap(out["high"], out["low"], out["close"], out["volume"])
    return out


def save_indicators(address: str, symbol: str, interval: str, df: pd.DataFrame) -> int:
    """Bulk insert indicator rows. Returns count inserted.

    Only writes rows newer than the latest stored indicator for this
    (address, interval) — historical rows never change, so rewriting them
    every cycle just burns time.
    """
    # Drop rows where the slowest indicator (EMA 200) is still NaN
    df_valid = df.dropna(subset=["ema_200"]).copy()
    if df_valid.empty:
        return 0

    conn = sqlite3.connect(DB_PATH)
    last_row = conn.execute("""
        SELECT MAX(timestamp) FROM indicators
        WHERE address = ? AND interval = ?
    """, (address, interval)).fetchone()
    conn.close()
    last_ts = last_row[0] if last_row and last_row[0] is not None else -1
    # Rewrite the last stored candle too, in case it was partial when computed.
    df_valid = df_valid[df_valid["timestamp"] >= last_ts]
    if df_valid.empty:
        return 0

    rows = []
    for _, r in df_valid.iterrows():
        rows.append((
            address, symbol, interval, int(r["timestamp"]), float(r["close"]),
            float(r["macd_line"]) if not pd.isna(r["macd_line"]) else None,
            float(r["macd_signal"]) if not pd.isna(r["macd_signal"]) else None,
            float(r["macd_hist"]) if not pd.isna(r["macd_hist"]) else None,
            float(r["stoch_rsi_k"]) if not pd.isna(r["stoch_rsi_k"]) else None,
            float(r["stoch_rsi_d"]) if not pd.isna(r["stoch_rsi_d"]) else None,
            float(r["bb_upper"]) if not pd.isna(r["bb_upper"]) else None,
            float(r["bb_middle"]) if not pd.isna(r["bb_middle"]) else None,
            float(r["bb_lower"]) if not pd.isna(r["bb_lower"]) else None,
            float(r["ema_50"]) if not pd.isna(r["ema_50"]) else None,
            float(r["ema_200"]) if not pd.isna(r["ema_200"]) else None,
            float(r["atr_14"]) if not pd.isna(r["atr_14"]) else None,
            float(r["volume_sma_20"]) if not pd.isna(r["volume_sma_20"]) else None,
            float(r["obv"]) if not pd.isna(r["obv"]) else None,
            float(r["vwap"]) if not pd.isna(r["vwap"]) else None,
        ))

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executemany("""
        INSERT OR REPLACE INTO indicators
        (address, symbol, interval, timestamp, close,
         macd_line, macd_signal, macd_hist, stoch_rsi_k, stoch_rsi_d,
         bb_upper, bb_middle, bb_lower, ema_50, ema_200,
         atr_14, volume_sma_20, obv, vwap)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    conn.close()
    return len(rows)


def get_token_intervals() -> list[tuple[str, str, str]]:
    """Return [(symbol, address, interval), ...] for token-intervals that need
    computing.

    Two filters keep this fast enough to never hit the pipeline timeout:
      1. Only tokens screened in the last 24h (whale_target only in whale-only
         mode) — not every token ever ingested.
      2. Skip token-intervals whose indicators already cover the latest candle
         (nothing new to compute).
    """
    conn = sqlite3.connect(DB_PATH)
    cutoff = int(time.time()) - SCREEN_LOOKBACK_SEC
    screen_filter = "AND s.screen = ?" if WHALE_ONLY else ""
    params: tuple = (cutoff, ACTIVE_SCREEN, MIN_CANDLES) if WHALE_ONLY else (cutoff, MIN_CANDLES)
    rows = conn.execute(f"""
        SELECT c.symbol, c.address, c.interval,
               MAX(c.timestamp) as last_candle_ts
        FROM candles c
        WHERE c.address IN (
            SELECT DISTINCT s.address FROM screened_tokens s
            WHERE s.screened_at >= ? {screen_filter}
            UNION
            SELECT address FROM paper_trades WHERE closed_at IS NULL
        )
        GROUP BY c.address, c.interval
        HAVING COUNT(*) >= ?
    """, params).fetchall()

    # Skip already-current token-intervals.
    todo = []
    for sym, addr, interval, last_candle_ts in rows:
        ind_row = conn.execute("""
            SELECT MAX(timestamp) FROM indicators
            WHERE address = ? AND interval = ?
        """, (addr, interval)).fetchone()
        last_ind_ts = ind_row[0] if ind_row and ind_row[0] is not None else -1
        if last_ind_ts < last_candle_ts:
            todo.append((sym, addr, interval))
    conn.close()
    return todo


def main_run(only_symbol: str | None):
    init_indicators_db()
    console = Console()
    console.print("[bold]Indicator Computer[/bold]\n")

    targets = get_token_intervals()
    if only_symbol:
        targets = [(s, a, i) for s, a, i in targets if s.upper() == only_symbol.upper()]

    if not targets:
        console.print("[green]All indicators already up to date (or no tokens with sufficient candles).[/green]")
        return

    console.print(f"Computing indicators for {len(targets)} token-interval combinations\n")

    summary = {}  # symbol -> {interval: rows_written}
    for sym, addr, interval in targets:
        df = load_candles(addr, interval)
        if len(df) < MIN_CANDLES:
            continue
        df_with_ind = compute_all_indicators(df)
        rows = save_indicators(addr, sym, interval, df_with_ind)
        summary.setdefault(sym, {})[interval] = rows

    # Display summary table
    table = Table(title="Indicators Computed")
    table.add_column("Symbol")
    table.add_column("5m", justify="right")
    table.add_column("15m", justify="right")
    table.add_column("1H", justify="right")
    table.add_column("4H", justify="right")
    table.add_column("1D", justify="right")
    table.add_column("Total rows", justify="right")

    for sym in sorted(summary.keys()):
        intervals = summary[sym]
        total = sum(intervals.values())
        table.add_row(
            sym[:12],
            str(intervals.get("5m", 0)),
            str(intervals.get("15m", 0)),
            str(intervals.get("1H", 0)),
            str(intervals.get("4H", 0)),
            str(intervals.get("1D", 0)),
            str(total),
        )
    console.print(table)

    # Sample latest values for one symbol — quick sanity check
    if summary:
        sample_sym = list(summary.keys())[0]
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("""
            SELECT i.interval, i.timestamp, i.close, i.macd_line, i.macd_signal,
                   i.stoch_rsi_k, i.bb_upper, i.bb_lower, i.ema_50, i.ema_200, i.atr_14
            FROM indicators i
            INNER JOIN (
                SELECT interval, MAX(timestamp) as max_ts
                FROM indicators
                WHERE symbol = ?
                GROUP BY interval
            ) latest ON i.interval = latest.interval AND i.timestamp = latest.max_ts
            WHERE i.symbol = ?
            ORDER BY i.interval
        """, (sample_sym, sample_sym)).fetchall()
        conn.close()

        if row:
            console.print(f"\n[bold]Latest indicators for {sample_sym} (sanity check):[/bold]")
            sample_table = Table()
            sample_table.add_column("Interval")
            sample_table.add_column("Close", justify="right")
            sample_table.add_column("MACD", justify="right")
            sample_table.add_column("Signal", justify="right")
            sample_table.add_column("StochRSI K", justify="right")
            sample_table.add_column("BB Upper", justify="right")
            sample_table.add_column("BB Lower", justify="right")
            sample_table.add_column("EMA 50", justify="right")
            sample_table.add_column("EMA 200", justify="right")
            sample_table.add_column("ATR", justify="right")
            for r in row:
                sample_table.add_row(
                    r[0],
                    f"{r[2]:.6f}" if r[2] else "-",
                    f"{r[3]:.6f}" if r[3] is not None else "-",
                    f"{r[4]:.6f}" if r[4] is not None else "-",
                    f"{r[5]:.1f}" if r[5] is not None else "-",
                    f"{r[6]:.6f}" if r[6] is not None else "-",
                    f"{r[7]:.6f}" if r[7] is not None else "-",
                    f"{r[8]:.6f}" if r[8] is not None else "-",
                    f"{r[9]:.6f}" if r[9] is not None else "-",
                    f"{r[10]:.6f}" if r[10] is not None else "-",
                )
            console.print(sample_table)

    console.print("\n[green]Done.[/green] Next: whale risk scoring")


@click.command()
@click.option("--only-symbol", default=None, help="Only compute for one symbol.")
def main(only_symbol):
    main_run(only_symbol)


if __name__ == "__main__":
    main()
