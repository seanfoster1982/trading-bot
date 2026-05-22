from pathlib import Path
import ast

p = Path("scripts/execution_coordinator.py")
src = p.read_text(encoding="utf-8")

before = src.count(r"\$")
src = src.replace(r"\$", "$")
after = src.count(r"\$")
removed = before - after

p.write_text(src, encoding="utf-8")
print(f"Removed {removed} stray backslash-dollar sequences")

try:
    ast.parse(src)
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
