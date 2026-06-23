import sqlite3
import shutil
import time
from pathlib import Path
from datetime import datetime, timezone

DB = Path("data/memecoins.db")

# 1. Back up the entire database with a timestamp
stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
backup = Path(f"data/memecoins_backup_{stamp}.db")
shutil.copy2(DB, backup)
print(f"Backed up full database to: {backup.name}")

# 2. Count what we are about to clear (so we have a record)
conn = sqlite3.connect(DB)
n_trades = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
n_closed = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE closed_at IS NOT NULL").fetchone()[0]
realized = conn.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM paper_trades WHERE closed_at IS NOT NULL").fetchone()[0]
print(f"About to clear: {n_trades} paper trades ({n_closed} closed, realized P&L ${realized:.2f})")

# 3. Clear ONLY paper_trades. Leave screened_tokens, candles, indicators,
#    signals, rug_reports, whale_signals, macro_regime, token_metadata intact.
conn.execute("DELETE FROM paper_trades")

# 4. Consume every existing BUY signal so the paper trader will not pick it up
#    after the reset. paper_trader.get_unfilled_buy_signals filters
#    WHERE s.action = 'BUY' AND no linked paper_trade; flipping action from
#    'BUY' to 'CONSUMED' excludes these rows from that query. Only NEW signals
#    that strategy.py writes after this reset (which will have action='BUY')
#    will produce new paper trades. This fixes the stale-signal replay bug:
#    previously, deleting paper_trades left every historical BUY signal as
#    "unfilled", so paper_trader would re-execute 7-9 day old signals against
#    current prices and record fictional -93% losses on tokens that had since
#    rugged (e.g. WCOR was replayed three times this way).
consumed_count = conn.execute(
    "UPDATE signals SET action = 'CONSUMED' WHERE action = 'BUY'"
).rowcount
print(f"Marked {consumed_count} historical BUY signals as CONSUMED (stale-signal fix)")

conn.commit()

# 5. Verify
remaining = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
candles = conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
signals = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
screened = conn.execute("SELECT COUNT(*) FROM screened_tokens").fetchone()[0]
conn.close()

print()
print("=== RESET COMPLETE ===")
print(f"  paper_trades:    {remaining} (cleared)")
print(f"  candles:         {candles:,} (preserved)")
print(f"  signals:         {signals:,} (preserved)")
print(f"  screened_tokens: {screened:,} (preserved)")
print()
print(f"Backup saved: {backup.name}")
print("Paper trading starts fresh at $100 with all risk fixes active.")
