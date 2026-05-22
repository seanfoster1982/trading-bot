from pathlib import Path
import ast

p = Path("scripts/monitor_loop.py")
src = p.read_text(encoding="utf-8")

# Update query_recent_signals to also check whether a paper trade exists for the signal
# This way, the alert only fires for signals that actually became new positions

old_query = """def query_recent_signals(since_ts: int) -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            \"\"\"SELECT symbol, strategy, entry_price, stop_loss, position_size_usd, generated_at
               FROM signals WHERE action = 'BUY' AND generated_at >= ?\"\"\",
            (since_ts,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [
        {'symbol': r[0], 'strategy': r[1], 'entry': r[2],
         'stop': r[3], 'size': r[4], 'ts': r[5]} for r in rows
    ]"""

new_query = """def query_recent_signals(since_ts: int) -> list[dict]:
    \"\"\"Only return signals that resulted in a NEW paper trade opening after since_ts.
    Filters out signals that were duplicates or rejected by dedupe.\"\"\"
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            \"\"\"SELECT pt.symbol, pt.strategy, pt.entry_price, pt.stop_loss,
                      pt.position_size_usd, pt.opened_at
               FROM paper_trades pt
               WHERE pt.opened_at >= ?
                 AND pt.close_reason IS NOT 'DEDUPE_CLEANUP'\"\"\",
            (since_ts,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [
        {'symbol': r[0], 'strategy': r[1], 'entry': r[2],
         'stop': r[3], 'size': r[4], 'ts': r[5]} for r in rows
    ]"""

if old_query in src:
    src = src.replace(old_query, new_query)
    print("Patched query_recent_signals to only count actual position opens")
else:
    print("WARN: function not found in expected form")

p.write_text(src, encoding="utf-8")

try:
    ast.parse(src)
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
