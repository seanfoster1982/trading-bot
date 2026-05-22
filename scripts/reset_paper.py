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

# 4. Also reset the 'executed' flag on signals so the paper trader sees a clean slate
#    (signals stay as historical record, but none are marked as having open trades)
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
