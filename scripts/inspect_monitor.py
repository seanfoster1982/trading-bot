from pathlib import Path
src = Path("scripts/monitor_loop.py").read_text(encoding="utf-8")
lines = src.splitlines()
for i, line in enumerate(lines):
    if "subprocess.run" in line:
        start = max(0, i - 1)
        end = min(len(lines), i + 18)
        for j in range(start, end):
            print(f"{j+1:4d}: {lines[j]}")
        break
