from pathlib import Path
import ast

p = Path("scripts/screen_memecoins.py")
src = p.read_text(encoding="utf-8")

old = 'sort_by="marketcap"'
new = 'sort_by="market_cap"'

count = src.count(old)
src = src.replace(old, new)
print(f"Fixed {count} occurrence(s): marketcap -> market_cap")

p.write_text(src, encoding="utf-8")

try:
    ast.parse(src.lstrip("\ufeff"))
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
