from pathlib import Path
import ast

p = Path("scripts/monitor_loop.py")
src = p.read_text(encoding="utf-8")

old = "    'screen_memecoins.py',\n    'rug_check.py',"
new = "    'screen_memecoins.py',\n    'watchlist.py',\n    'rug_check.py',"

if old in src:
    src = src.replace(old, new)
    p.write_text(src, encoding="utf-8")
    print("Added watchlist.py to pipeline after screen_memecoins.py")
else:
    print("WARN: pipeline insert point not found - showing current PIPELINE block:")
    for i, line in enumerate(src.splitlines(), 1):
        if 37 <= i <= 47:
            print(f"  {i}: {line!r}")

try:
    ast.parse(src.lstrip("\ufeff"))
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
