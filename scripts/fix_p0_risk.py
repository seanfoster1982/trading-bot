from pathlib import Path
import ast

p = Path("scripts/paper_trader.py")
src = p.read_text(encoding="utf-8")

# === FIX 1: Capital cap. Add a constant and a helper that computes deployed capital. ===
# Insert constants + helper right before open_position()

helper = """# === RISK MANAGEMENT (added 2026-05-21 after WCOR disaster) ===
START_CAPITAL_USD = 100.0
MAX_TOTAL_EXPOSURE_USD = 100.0   # never deploy more than this across all open positions
MAX_LOSS_PCT_HARD = 0.15         # force-close any position down more than 15% at check time


def get_deployed_capital() -> float:
    \"\"\"Sum of position_size_usd across all currently-open paper trades.\"\"\"
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(SUM(position_size_usd), 0) FROM paper_trades WHERE closed_at IS NULL"
    ).fetchone()
    conn.close()
    return float(row[0]) if row else 0.0


def get_realized_pnl() -> float:
    \"\"\"Sum of realized P&L across all closed paper trades.\"\"\"
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl_usd), 0) FROM paper_trades WHERE closed_at IS NOT NULL"
    ).fetchone()
    conn.close()
    return float(row[0]) if row else 0.0


def available_capital() -> float:
    \"\"\"Capital free to deploy: start + realized P&L - currently deployed.\"\"\"
    return START_CAPITAL_USD + get_realized_pnl() - get_deployed_capital()


"""

marker = "def open_position(sig):"
if marker in src and "get_deployed_capital" not in src:
    src = src.replace(marker, helper + marker)
    print("FIX 1: Added capital tracking helpers")
else:
    print("FIX 1: SKIP (helpers already present or marker missing)")

# === FIX 2: open_position checks available capital before opening ===
old_open = '''    tokens = position_usd / entry_price
    conn = sqlite3.connect(DB_PATH)'''
new_open = '''    # CAPITAL CAP: refuse to open if not enough free capital
    avail = available_capital()
    if position_usd > avail:
        return False, f"insufficient capital (need ${position_usd:.2f}, have ${avail:.2f})"

    tokens = position_usd / entry_price
    conn = sqlite3.connect(DB_PATH)'''

if old_open in src:
    src = src.replace(old_open, new_open)
    print("FIX 2: open_position now checks available capital")
else:
    print("FIX 2: WARN - open_position body not found in expected form")

# === FIX 3: hard max-loss circuit breaker in check_position_exits ===
# We need to see check_position_exits first - inject a hard stop at the top of it.
old_exits = "def check_position_exits(pos, current_price, current_ts):"
# Find the function and inject a hard-loss check. We do it by inserting after the def line + docstring.
lines = src.splitlines()
out = []
i = 0
injected = False
while i < len(lines):
    out.append(lines[i])
    if lines[i].strip().startswith("def check_position_exits(") and not injected:
        j = i + 1
        # copy blank lines
        while j < len(lines) and not lines[j].strip():
            out.append(lines[j]); j += 1
        # copy docstring if present
        if j < len(lines) and (lines[j].lstrip().startswith('"""') or lines[j].lstrip().startswith("'''")):
            q = '"""' if '"""' in lines[j] else "'''"
            out.append(lines[j])
            if lines[j].count(q) >= 2:
                j += 1
            else:
                j += 1
                while j < len(lines) and q not in lines[j]:
                    out.append(lines[j]); j += 1
                if j < len(lines):
                    out.append(lines[j]); j += 1
        # inject hard-loss check
        block = [
            "    # HARD MAX-LOSS CIRCUIT BREAKER (added 2026-05-21)",
            "    if current_price and current_price > 0 and pos.get('entry_price'):",
            "        loss_pct = (current_price - pos['entry_price']) / pos['entry_price']",
            "        if loss_pct <= -MAX_LOSS_PCT_HARD:",
            "            return True, f'HARD_STOP at {loss_pct*100:.1f}% (max -{MAX_LOSS_PCT_HARD*100:.0f}%)'",
            "",
        ]
        out.extend(block)
        injected = True
        i = j
        continue
    i += 1
src = "\n".join(out)
print(f"FIX 3: Hard max-loss breaker injected: {injected}")

p.write_text(src, encoding="utf-8")

try:
    ast.parse(src.lstrip("\ufeff"))
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error at line {e.lineno}: {e.text}")
