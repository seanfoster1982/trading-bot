from pathlib import Path
import ast

p = Path("scripts/strategy.py")
src = p.read_text(encoding="utf-8")

# Find evaluate_token function and inject cooldown check right after the def line.
# Strategy: look for "def evaluate_token" and insert helper + early-return right after the signature.

# First, add the cooldown helper function before evaluate_token
helper = '''
# Cooldown rule: after a STOP_LOSS exit on a token, do not re-enter for 24 hours.
# Added 2026-05-15 after observing repeated same-token stop-outs (ASTEROID, TROLL, BULL).
COOLDOWN_HOURS_AFTER_STOPLOSS = 24


def recent_stoploss_cooldown(address: str) -> tuple[bool, float]:
    """Check if this token had a STOP_LOSS exit within the cooldown window.
    Returns (is_in_cooldown, hours_remaining)."""
    import time
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT MAX(closed_at) FROM paper_trades
        WHERE address = ?
          AND close_reason LIKE 'STOP_LOSS%'
    """, (address,)).fetchone()
    conn.close()
    if not row or not row[0]:
        return (False, 0.0)
    last_stop_ts = int(row[0])
    age_hours = (time.time() - last_stop_ts) / 3600
    if age_hours < COOLDOWN_HOURS_AFTER_STOPLOSS:
        remaining = COOLDOWN_HOURS_AFTER_STOPLOSS - age_hours
        return (True, remaining)
    return (False, 0.0)


'''

# Insert helper just before evaluate_token definition
marker = "def evaluate_token(address: str, symbol: str, screen: str) -> Signal:"
if marker not in src:
    print("ERROR: evaluate_token function signature not found - aborting")
    raise SystemExit(1)

src = src.replace(marker, helper + marker)
print("Inserted cooldown helper")

# Now find the body of evaluate_token and inject the early-return check.
# We need to add the check as the first thing inside the function.
# Find the line after the function signature (could be docstring or first code line).
lines = src.splitlines()
out_lines = []
i = 0
injected = False
while i < len(lines):
    out_lines.append(lines[i])
    if lines[i].strip().startswith("def evaluate_token(address: str, symbol: str, screen: str)") and not injected:
        # Find the first line of the function body (skip docstring if present)
        j = i + 1
        # Skip blank lines
        while j < len(lines) and not lines[j].strip():
            out_lines.append(lines[j])
            j += 1
        # Skip docstring if present
        if j < len(lines) and (lines[j].strip().startswith('"""') or lines[j].strip().startswith("'''")):
            quote = '"""' if '"""' in lines[j] else "'''"
            out_lines.append(lines[j])
            # If docstring is single-line, we're done
            if lines[j].count(quote) >= 2:
                j += 1
            else:
                j += 1
                # Multi-line docstring - copy until closing quote
                while j < len(lines) and quote not in lines[j]:
                    out_lines.append(lines[j])
                    j += 1
                if j < len(lines):
                    out_lines.append(lines[j])
                    j += 1
        # Inject cooldown check right here
        cooldown_block = [
            "    # COOLDOWN CHECK: skip if this token had a STOP_LOSS within last 24h",
            "    in_cd, hours_remaining = recent_stoploss_cooldown(address)",
            "    if in_cd:",
            "        return Signal(",
            "            address=address, symbol=symbol, action='HOLD',",
            "            strategy=screen_to_strategy(screen),",
            "            entry_price=0.0, stop_loss=0.0, take_profit=0.0,",
            "            position_size_usd=0.0,",
            "            reason=f'COOLDOWN: stop-out within {COOLDOWN_HOURS_AFTER_STOPLOSS}h ({hours_remaining:.1f}h remaining)',",
            "        )",
            "",
        ]
        out_lines.extend(cooldown_block)
        injected = True
        i = j
        continue
    i += 1

src = "\n".join(out_lines)
print(f"Injected cooldown early-return: {injected}")

# Check if screen_to_strategy exists - if not, we need a fallback
if "screen_to_strategy" not in src:
    print("WARN: screen_to_strategy() helper not found. Using inline mapping.")
    src = src.replace(
        "strategy=screen_to_strategy(screen),",
        "strategy={'A': 'momentum', 'B': 'whale_copy', 'C': 'lottery'}.get(screen, 'momentum'),"
    )
    print("  Replaced with inline screen-to-strategy mapping")

p.write_text(src, encoding="utf-8")

try:
    ast.parse(src)
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error at line {e.lineno}: {e.text}")
    raise SystemExit(1)

# Verify the cooldown check is in place by counting occurrences of the marker
verify_count = src.count("recent_stoploss_cooldown")
print(f"Cooldown function references: {verify_count} (expected 2: definition + call)")
