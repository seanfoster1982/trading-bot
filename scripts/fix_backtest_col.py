from pathlib import Path
import ast

p = Path("scripts/backtest.py")
src = p.read_text(encoding="utf-8")

old = "ORDER BY discovered_at DESC LIMIT 1"
new = "ORDER BY screened_at DESC LIMIT 1"

if old in src:
    src = src.replace(old, new)
    p.write_text(src, encoding="utf-8")
    print("Patched: discovered_at -> screened_at")
else:
    print("WARN: target line not found - manual check needed")

try:
    ast.parse(src)
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
