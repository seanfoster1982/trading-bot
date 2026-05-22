from pathlib import Path
import ast

p = Path("scripts/jupiter_quote.py")
src = p.read_text(encoding="utf-8")

# Strip the bad backslashes inside f-strings — same fix as paper_trader.py last night
replacements = [
    ("sig[\\'symbol\\']", "sig['symbol']"),
    ("\\'BUY\\'", "'BUY'"),
    ("\\'15m\\'", "'15m'"),
    ("sig[\\'entry_price\\']", "sig['entry_price']"),
    ("sig[\\'address\\']", "sig['address']"),
]

n_replaced = 0
for old, new in replacements:
    count = src.count(old)
    if count:
        src = src.replace(old, new)
        n_replaced += count
        print(f"Replaced {count}x: {old}")

p.write_text(src, encoding="utf-8")
print(f"Total: {n_replaced} replacements")

# Verify syntax
try:
    ast.parse(src)
    print("Syntax OK")
except SyntaxError as e:
    print(f"STILL HAS SYNTAX ERROR: {e}")
