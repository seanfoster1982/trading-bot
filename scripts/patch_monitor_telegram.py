from pathlib import Path
import ast

p = Path("scripts/monitor_loop.py")
src = p.read_text(encoding="utf-8")

# Add import for the notifier at the top, near other imports
import_marker = "from datetime import datetime, timezone"
if "from scripts.telegram_notifier import send as tg_send" not in src:
    if import_marker in src:
        src = src.replace(
            import_marker,
            import_marker + "\n\n# Telegram notifier - no-ops if env vars not set\nsys_path_inserted = True\nimport sys as _sys\nfrom pathlib import Path as _Path\n_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))\nfrom scripts.telegram_notifier import send as tg_send, is_configured as tg_configured"
        )
        print("Added telegram import")
    else:
        print("WARN: import marker not found")

# Replace write_alerts to also send Telegram
old_write_alerts = """def write_alerts(summary: dict) -> None:
    \"\"\"Write notable events to the alerts log.\"\"\""""

new_write_alerts = """def write_alerts(summary: dict) -> None:
    \"\"\"Write notable events to the alerts log AND send to Telegram.\"\"\"
    tg_on = tg_configured()"""

if old_write_alerts in src:
    src = src.replace(old_write_alerts, new_write_alerts)
    print("Patched write_alerts header")
else:
    print("WARN: write_alerts function not found in expected form")

# Add Telegram send call after each alert log append
# Three places to patch: BUY_SIGNAL, POSITION_CLOSED, ERROR

old_buy = """    for sig in summary['new_signals']:
        append_log(ALERT_PATH, ("""
new_buy = """    for sig in summary['new_signals']:
        if tg_on:
            tg_send(
                f"<b>BUY SIGNAL</b>\\n"
                f"<b>{sig['symbol']}</b> ({sig['strategy']})\\n"
                f"Entry: {sig['entry']:.8f}\\n"
                f"Stop: {sig['stop']:.8f}\\n"
                f"Size: ${sig['size']:.2f}"
            )
        append_log(ALERT_PATH, ("""

if old_buy in src:
    src = src.replace(old_buy, new_buy)
    print("Patched BUY signal notifier")

old_close = """    for cl in summary['new_closes']:
        sign = '+' if cl['pnl_usd'] >= 0 else ''
        append_log(ALERT_PATH, ("""
new_close = """    for cl in summary['new_closes']:
        sign = '+' if cl['pnl_usd'] >= 0 else ''
        if tg_on:
            emoji = "✓" if cl['pnl_usd'] >= 0 else "✗"
            tg_send(
                f"<b>{emoji} POSITION CLOSED</b>\\n"
                f"<b>{cl['symbol']}</b> ({cl['strategy']})\\n"
                f"P&L: {sign}${cl['pnl_usd']:.2f} ({sign}{cl['pnl_pct']:.1f}%)\\n"
                f"Reason: {cl['reason']}"
            )
        append_log(ALERT_PATH, ("""

if old_close in src:
    src = src.replace(old_close, new_close)
    print("Patched POSITION_CLOSED notifier")

old_err = """    for err in summary['errors']:
        append_log(ALERT_PATH, f\"{ts()} | ERROR | {err}\")"""
new_err = """    for err in summary['errors']:
        if tg_on:
            tg_send(f"<b>BOT ERROR</b>\\n<code>{err[:300]}</code>", silent=True)
        append_log(ALERT_PATH, f\"{ts()} | ERROR | {err}\")"""

if old_err in src:
    src = src.replace(old_err, new_err)
    print("Patched ERROR notifier")

p.write_text(src, encoding="utf-8")

try:
    ast.parse(src)
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
