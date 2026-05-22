from pathlib import Path

p = Path("scripts/monitor_loop.py")
lines = p.read_text(encoding="utf-8").splitlines()

# Delete the three duplicate lines (indices 64-66 = lines 65-67)
duplicates_to_remove = [
    "            encoding='utf-8',",
    "            errors='replace',",
    "            env={**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8'},",
]

# Find the second occurrence of each and remove it (keep first occurrence)
seen = {line: 0 for line in duplicates_to_remove}
new_lines = []
for line in lines:
    stripped_match = line in duplicates_to_remove
    if stripped_match:
        seen[line] += 1
        if seen[line] > 1:
            # Skip duplicate
            continue
    new_lines.append(line)

p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

# Verify
import ast
try:
    ast.parse(Path("scripts/monitor_loop.py").read_text(encoding="utf-8"))
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error remains: {e}")

# Show the fixed block
src = Path("scripts/monitor_loop.py").read_text(encoding="utf-8")
fixed_lines = src.splitlines()
for i, line in enumerate(fixed_lines):
    if "subprocess.run" in line:
        for j in range(i, min(len(fixed_lines), i + 14)):
            print(f"{j+1:4d}: {fixed_lines[j]}")
        break
