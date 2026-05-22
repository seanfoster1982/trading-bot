from pathlib import Path
import ast

p = Path("scripts/strategy.py")
src = p.read_text(encoding="utf-8")

old = 'return False, f"5m close ({close:.6f}) not above VWAP ({vwap:.6f if vwap else 0})"'
new = 'vwap_str = f"{vwap:.6f}" if vwap else "0"\n        return False, f"5m close ({close:.6f}) not above VWAP ({vwap_str})"'

if old in src:
    src = src.replace(old, new)
    p.write_text(src, encoding="utf-8")
    print("Fixed malformed f-string in confirm_5m")
else:
    print("WARN: target line not found - showing line 276 area:")
    for i, line in enumerate(src.splitlines(), 1):
        if 273 <= i <= 279:
            print(f"  {i}: {line!r}")

try:
    ast.parse(src.lstrip("\ufeff"))
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
