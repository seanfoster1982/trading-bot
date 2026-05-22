from pathlib import Path
import ast

p = Path("scripts/strategy.py")
src = p.read_text(encoding="utf-8")

old = "reason=f'COOLDOWN: stop-out within {COOLDOWN_HOURS_AFTER_STOPLOSS}h ({hours_remaining:.1f}h remaining)',"
new = "reasoning=f'COOLDOWN: stop-out within {COOLDOWN_HOURS_AFTER_STOPLOSS}h ({hours_remaining:.1f}h remaining)',"

count = src.count(old)
src = src.replace(old, new)
print(f"Replaced {count} occurrence: reason -> reasoning")

p.write_text(src, encoding="utf-8")

try:
    ast.parse(src.lstrip("\ufeff"))
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
