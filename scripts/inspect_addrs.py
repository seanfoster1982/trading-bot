import sqlite3
conn = sqlite3.connect("data/memecoins.db")

# Get full addresses for the test tokens
for sym in ["TROLL", "ASTEROID", "BULL"]:
    row = conn.execute(
        "SELECT symbol, address, LENGTH(address) FROM screened_tokens WHERE symbol = ? LIMIT 1",
        (sym,)
    ).fetchone()
    if row:
        print(f"{row[0]}: addr={row[1]!r} length={row[2]}")
    else:
        print(f"{sym}: not found in screened_tokens")

conn.close()
