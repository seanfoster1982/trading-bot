import sqlite3
conn = sqlite3.connect("data/memecoins.db")
cols = conn.execute("PRAGMA table_info(indicators)").fetchall()
print("indicators table columns:")
for c in cols:
    print(f"  {c[1]} ({c[2]})")
conn.close()
