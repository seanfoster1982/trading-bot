from pathlib import Path
import ast

p = Path("scripts/strategy.py")
src = p.read_text(encoding="utf-8")

# Find a good place to add the cooldown check - right where we evaluate each token.
# We need to see strategy.py first to know what the structure looks like.
print("Reading current strategy.py structure...")
lines = src.splitlines()
for i, line in enumerate(lines):
    if "def " in line and not line.strip().startswith("#"):
        print(f"  line {i+1}: {line.strip()[:100]}")
