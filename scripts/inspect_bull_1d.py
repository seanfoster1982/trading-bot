import sqlite3
import pandas as pd

conn = sqlite3.connect("data/memecoins.db")

# Get BULL address
row = conn.execute("SELECT address FROM screened_tokens WHERE symbol = ?", ("BULL",)).fetchone()
addr = row[0]
print(f"BULL address: {addr}\n")

# How many 1D candles do we have for BULL total?
total_1d = conn.execute(
    "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM candles WHERE address = ? AND interval = '1D'",
    (addr,)
).fetchone()
print(f"Total 1D candles for BULL: {total_1d[0]}")
if total_1d[1]:
    from datetime import datetime, timezone
    earliest = datetime.fromtimestamp(total_1d[1], timezone.utc).strftime('%Y-%m-%d')
    latest = datetime.fromtimestamp(total_1d[2], timezone.utc).strftime('%Y-%m-%d')
    print(f"Date range: {earliest} to {latest}")
print()

# What does the indicators table say for BULL's 1D?
ind_count = conn.execute(
    "SELECT COUNT(*) FROM indicators WHERE address = ? AND interval = '1D'",
    (addr,)
).fetchone()[0]
print(f"Total 1D indicator rows for BULL: {ind_count}")

# Last 5 rows of indicators for BULL 1D
ind = pd.read_sql_query("""
    SELECT timestamp, close, ema_50, ema_200, atr
    FROM indicators
    WHERE address = ? AND interval = '1D'
    ORDER BY timestamp DESC LIMIT 5
""", conn, params=(addr,))
print(f"\nLast 5 1D indicators (from live strategy's computation):")
for _, row in ind.iterrows():
    is_bullish = (row['close'] > row['ema_50'] > row['ema_200']) if pd.notna(row['ema_200']) else False
    print(f"  ts={row['timestamp']} close={row['close']:.8f} ema50={row['ema_50']} ema200={row['ema_200']} bullish={is_bullish}")

# Last 5 raw 1D candles
candles = pd.read_sql_query("""
    SELECT timestamp, open, high, low, close
    FROM candles
    WHERE address = ? AND interval = '1D'
    ORDER BY timestamp DESC LIMIT 5
""", conn, params=(addr,))
print(f"\nLast 5 1D raw candles:")
for _, row in candles.iterrows():
    print(f"  ts={row['timestamp']} close={row['close']:.8f}")

conn.close()
