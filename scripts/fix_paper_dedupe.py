from pathlib import Path
import sqlite3
import time

# ============================================================
# Fix 1: Patch paper_trader.py to skip signals with existing open position
# ============================================================
p = Path("scripts/paper_trader.py")
src = p.read_text(encoding="utf-8")

old_open_loop = """    if new_signals:
        console.print(f\"[cyan]Opening positions for {len(new_signals)} new signal(s)...[/cyan]\")
        for sig in new_signals:
            if dry_run:
                console.print(f\"  [dim](dry-run) would open {sig['symbol']} at {sig['entry_price']:.8f}[/dim]\")
                continue
            ok, msg = open_position(sig)
            mark = \"[green]OK[/green]\" if ok else \"[red]FAIL[/red]\"
            console.print(f\"  {mark} {sig['symbol']}: {msg}\")
            if ok:
                opened.append(sig[\"symbol\"])"""

new_open_loop = """    if new_signals:
        console.print(f\"[cyan]Opening positions for {len(new_signals)} new signal(s)...[/cyan]\")
        # Get addresses with existing open positions to prevent duplicates
        conn_check = sqlite3.connect(DB_PATH)
        open_addresses = {r[0] for r in conn_check.execute(
            \"SELECT address FROM paper_trades WHERE closed_at IS NULL\"
        ).fetchall()}
        conn_check.close()
        for sig in new_signals:
            if sig[\"address\"] in open_addresses:
                console.print(f\"  [yellow]SKIP[/yellow] {sig['symbol']}: already has open position\")
                continue
            if dry_run:
                console.print(f\"  [dim](dry-run) would open {sig['symbol']} at {sig['entry_price']:.8f}[/dim]\")
                continue
            ok, msg = open_position(sig)
            mark = \"[green]OK[/green]\" if ok else \"[red]FAIL[/red]\"
            console.print(f\"  {mark} {sig['symbol']}: {msg}\")
            if ok:
                opened.append(sig[\"symbol\"])
                open_addresses.add(sig[\"address\"])"""

if old_open_loop in src:
    src = src.replace(old_open_loop, new_open_loop)
    print("Fix 1 applied: paper trader deduplicates open positions per token")
else:
    print("WARN: open loop block not found — paper trader may be in different shape")

p.write_text(src, encoding="utf-8")

# ============================================================
# Fix 2: Close the older duplicate BULL position
# ============================================================
conn = sqlite3.connect("data/memecoins.db")
cur = conn.cursor()

# Find duplicates: tokens with multiple open positions
dupes = cur.execute("""
    SELECT address, symbol, COUNT(*) as n
    FROM paper_trades
    WHERE closed_at IS NULL
    GROUP BY address
    HAVING n > 1
""").fetchall()

print(f"Found {len(dupes)} tokens with duplicate open positions")
for addr, sym, n in dupes:
    print(f"  {sym}: {n} duplicates — keeping newest, closing the rest")
    # Get all open positions for this token, ordered by opened_at desc
    rows = cur.execute("""
        SELECT id, opened_at FROM paper_trades
        WHERE address = ? AND closed_at IS NULL
        ORDER BY opened_at DESC
    """, (addr,)).fetchall()
    # Keep the first (newest), close the rest with DEDUPE_CLEANUP reason
    for trade_id, opened_at in rows[1:]:
        cur.execute("""
            UPDATE paper_trades
            SET closed_at = ?,
                close_price = entry_price,
                close_reason = "DEDUPE_CLEANUP",
                pnl_usd = 0,
                pnl_pct = 0
            WHERE id = ?
        """, (int(time.time()), trade_id))
        print(f"    closed duplicate trade id={trade_id} (opened {opened_at})")

conn.commit()
conn.close()
print("Dedupe cleanup complete")
