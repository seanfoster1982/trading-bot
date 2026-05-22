import sqlite3
conn = sqlite3.connect("data/memecoins.db")
cols = conn.execute("PRAGMA table_info(screened_tokens)").fetchall()
print("screened_tokens columns:")
for col in cols:
    print(f"  {col[1]}  ({col[2]})")
conn.close()
