from pathlib import Path

p = Path('scripts/paper_trader.py')
src = p.read_text(encoding='utf-8')

# Fix all the escaped single quotes inside f-strings
# The writer added backslashes that don't belong inside Python f-strings
replacements = [
    ("sig[\\'symbol\\']", "sig['symbol']"),
    ("sig[\\'entry_price\\']", "sig['entry_price']"),
    ("pos[\\'symbol\\']", "pos['symbol']"),
    ("pos[\\'entry_price\\']", "pos['entry_price']"),
    ("pos[\\'address\\']", "pos['address']"),
    ("\\'BUY\\'", "'BUY'"),
    ("doesn\\'t", "doesnt"),
]

n_replaced = 0
for old, new in replacements:
    count = src.count(old)
    if count:
        src = src.replace(old, new)
        n_replaced += count
        print(f"Replaced {count}x: {old} -> {new}")

p.write_text(src, encoding='utf-8')
print(f"Total replacements: {n_replaced}")
print("PATCHED")
