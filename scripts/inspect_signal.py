from pathlib import Path
src = Path("scripts/strategy.py").read_text(encoding="utf-8")
lines = src.splitlines()
for i, line in enumerate(lines):
    if "class Signal" in line:
        for j in range(i, min(len(lines), i + 40)):
            print(f"{j+1:4d}: {lines[j]}")
        break
