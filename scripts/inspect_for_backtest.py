import sqlite3
conn = sqlite3.connect("data/memecoins.db")
for table in ["candles", "indicators", "screened_tokens"]:
    print(f"\n=== {table} ===")
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    for c in cols:
        print(f"  {c[1]} ({c[2]})")
    # row count and timestamp range
    try:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  ROWS: {n:,}")
    except Exception as e:
        print(f"  (count failed: {e})")

# How many tokens have enough candle history to backtest?
print("\n=== Backtest-able tokens ===")
rows = conn.execute("""
    SELECT address, COUNT(*) as n
    FROM candles WHERE interval='5m'
    GROUP BY address
    HAVING n >= 500
    ORDER BY n DESC
    LIMIT 10
""").fetchall()
print(f"Tokens with 500+ 5m candles (showing top 10):")
for addr, n in rows:
    sym = conn.execute("SELECT symbol FROM screened_tokens WHERE address=? LIMIT 1", (addr,)).fetchone()
    sym = sym[0] if sym else "?"
    print(f"  {sym:12s} {n:,} candles  {addr[:12]}...")

total = conn.execute("""
    SELECT COUNT(*) FROM (
        SELECT address FROM candles WHERE interval='5m'
        GROUP BY address HAVING COUNT(*) >= 500
    )
""").fetchone()[0]
print(f"\nTotal tokens with 500+ 5m candles: {total}")
conn.close()
