from pathlib import Path
import ast

for fname in ["scripts/daily_report.py"]:
    p = Path(fname)
    src = p.read_text(encoding="utf-8")
    n_before = src.count(r"\$")
    src = src.replace(r"\$", "$")
    n_after = src.count(r"\$")
    p.write_text(src, encoding="utf-8")
    print(f"{fname}: removed {n_before - n_after} stray backslash-dollar sequences")
    ast.parse(src)
print("All files Syntax OK")
