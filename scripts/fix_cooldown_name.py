from pathlib import Path
import ast

p = Path("scripts/strategy.py")
src = p.read_text(encoding="utf-8")

# Unconditional fix: replace screen_to_strategy call with inline dict mapping
old = "strategy=screen_to_strategy(screen),"
new = "strategy={'A': 'momentum', 'B': 'whale_copy', 'C': 'lottery'}.get(screen, 'momentum'),"

count = src.count(old)
src = src.replace(old, new)
print(f"Replaced {count} call(s) to screen_to_strategy with inline mapping")

p.write_text(src, encoding="utf-8")

# Parse without BOM tripping us up
try:
    ast.parse(src.lstrip("\ufeff"))
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
