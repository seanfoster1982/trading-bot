"""Weekly clean-run check.

Run this any time to see how many clean post-reset paper trades have closed
and what the win rate is. Compares against the section-12 thresholds in
HANDOFF.md (45%+ keep refining / 35% or below stop iterating).

Usage:
    cd C:\\Users\\seanf\\Documents\\trading-bot
    .\\.venv\\Scripts\\python.exe check_clean.py
"""
import sqlite3
import datetime

c = sqlite3.connect("data/memecoins.db")

print("=== Clean paper trades (all post-reset) ===")
r = c.execute("""
    SELECT COUNT(*) n,
           SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins,
           SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) losses,
           ROUND(SUM(pnl_usd), 2) total_pnl,
           ROUND(AVG(pnl_usd), 2) avg_pnl
    FROM paper_trades
    WHERE closed_at IS NOT NULL
""").fetchone()
n, wins, losses, total, avg = r
wins = wins or 0
losses = losses or 0
wr = (100 * wins / n) if n else 0
print(f"  Closed trades: {n}")
print(f"  Wins: {wins}   Losses: {losses}   Win rate: {wr:.1f}%")
print(f"  Total P&L: ${total or 0:+.2f}   Avg per trade: ${avg or 0:+.2f}")

print()
print("=== By strategy ===")
for row in c.execute("""
    SELECT strategy, COUNT(*) n,
           SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins,
           ROUND(SUM(pnl_usd), 2) total
    FROM paper_trades WHERE closed_at IS NOT NULL
    GROUP BY strategy
""").fetchall():
    strat, n_s, w_s, t_s = row
    w_s = w_s or 0
    wr_s = (100 * w_s / n_s) if n_s else 0
    print(f"  {strat:<12} n={n_s}  wins={w_s}  wr={wr_s:.0f}%  total=${t_s or 0:+.2f}")

print()
print("=== Open positions right now ===")
now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
opens = c.execute("""
    SELECT symbol, strategy, opened_at, position_size_usd
    FROM paper_trades WHERE closed_at IS NULL
""").fetchall()
if not opens:
    print("  (none)")
for row in opens:
    sym, strat, op, sz = row
    age_h = (now_ts - op) / 3600
    print(f"  {sym:<10} {strat:<12} age={age_h:.1f}h  size=${sz}")

print()
print("=== Trades that used the 5% fallback stop (ATR was missing) ===")
fb = c.execute("""
    SELECT COUNT(*) n,
           SUM(CASE WHEN pt.pnl_usd > 0 THEN 1 ELSE 0 END) wins,
           ROUND(SUM(pt.pnl_usd), 2) total
    FROM paper_trades pt
    JOIN signals s ON s.id = pt.signal_id
    WHERE pt.closed_at IS NOT NULL
      AND s.reasoning_json LIKE '%STOP_FALLBACK_5PCT%'
""").fetchone()
real = c.execute("""
    SELECT COUNT(*) n,
           SUM(CASE WHEN pt.pnl_usd > 0 THEN 1 ELSE 0 END) wins,
           ROUND(SUM(pt.pnl_usd), 2) total
    FROM paper_trades pt
    JOIN signals s ON s.id = pt.signal_id
    WHERE pt.closed_at IS NOT NULL
      AND s.reasoning_json NOT LIKE '%STOP_FALLBACK_5PCT%'
""").fetchone()
n_fb, fb_wins, fb_total = fb
n_real, real_wins, real_total = real
fb_wins = fb_wins or 0
real_wins = real_wins or 0
fb_wr = (100 * fb_wins / n_fb) if n_fb else 0
real_wr = (100 * real_wins / n_real) if n_real else 0
print(f"  Fallback-stop trades:   n={n_fb}  wins={fb_wins}  wr={fb_wr:.0f}%  total=${fb_total or 0:+.2f}")
print(f"  Real ATR-stop trades:   n={n_real}  wins={real_wins}  wr={real_wr:.0f}%  total=${real_total or 0:+.2f}")

print()
print("=== Decision rule (per HANDOFF.md section 12) ===")
if n >= 30:
    if wr >= 45:
        print(f"  Win rate {wr:.0f}% -> may have edge, keep refining.")
    elif wr <= 35:
        print(f"  Win rate {wr:.0f}% -> backtest was optimism; do not go live with this entry logic.")
    else:
        print(f"  Win rate {wr:.0f}% -> grey zone; collect more trades or compare ATR-real vs fallback split.")
else:
    print(f"  Only {n} closed trades so far. Need 30-50 for the win rate to be meaningful.")

c.close()
