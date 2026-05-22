import sqlite3
import time
from datetime import datetime, timezone

DUST = "6veQU7HDdXV5DC2Eqhnri5q71gkMzG73qKkSSudnpump"
BABYTROLL = "6qdzMx4c9rL2X3Ns3SwZ8uEo4zReDPjdXpAEmpo7pump"

conn = sqlite3.connect("data/memecoins.db")

for label, addr in [("DUST", DUST), ("BABYTROLL", BABYTROLL)]:
    print(f"\n=== {label} ({addr[:12]}...) ===")

    rows = conn.execute("""
        SELECT screen, symbol, market_cap, liquidity, volume_24h, price,
               price_change_24h, holder_count, listed_at, screened_at,
               price_change_1h, price_change_4h
        FROM screened_tokens WHERE address = ?
        ORDER BY screened_at DESC LIMIT 5
    """, (addr,)).fetchall()

    if not rows:
        print("  NOT FOUND in screened_tokens table.")
        print("  Means: never passed any of the 3 screen filters in our history.")
        continue

    print(f"  Found {len(rows)} screened entries (most recent first):")
    for row in rows:
        screen, symbol, mcap, liq, vol, price, ch24, holders, listed, screened, ch1h, ch4h = row
        screened_str = datetime.fromtimestamp(screened, timezone.utc).strftime('%m-%d %H:%M UTC')
        listed_str = datetime.fromtimestamp(listed, timezone.utc).strftime('%m-%d') if listed else 'N/A'
        print(f"    [{screened_str}] screen={screen} symbol={symbol}")
        print(f"      mcap=${mcap:,.0f} liq=${liq:,.0f} vol24h=${vol:,.0f}")
        p_str = f"${price:.8f}" if price is not None else "N/A"
        c1 = f"{ch1h:+.1f}%" if ch1h is not None else "N/A"
        c4 = f"{ch4h:+.1f}%" if ch4h is not None else "N/A"
        c24 = f"{ch24:+.1f}%" if ch24 is not None else "N/A"
        print(f"      price={p_str}  1h={c1}  4h={c4}  24h={c24}")
        print(f"      holders={holders:,} listed={listed_str}")

    # Did it ever generate a signal?
    sig_rows = conn.execute("""
        SELECT generated_at, strategy, action, entry_price
        FROM signals WHERE address = ?
        ORDER BY generated_at DESC LIMIT 3
    """, (addr,)).fetchall()
    if sig_rows:
        print(f"  Signals fired: {len(sig_rows)} times")
        for ts, strat, act, ep in sig_rows:
            ts_str = datetime.fromtimestamp(ts, timezone.utc).strftime('%m-%d %H:%M UTC')
            print(f"    [{ts_str}] {act} via {strat} @ {ep:.8f}")
    else:
        print(f"  Signals fired: NEVER")

    # Did it ever generate a paper trade?
    trade_rows = conn.execute("""
        SELECT opened_at, closed_at, entry_price, close_price, pnl_usd, close_reason
        FROM paper_trades WHERE address = ?
        ORDER BY opened_at DESC LIMIT 3
    """, (addr,)).fetchall()
    if trade_rows:
        print(f"  Paper trades: {len(trade_rows)}")
        for op, cl, ep, xp, pnl, reason in trade_rows:
            op_str = datetime.fromtimestamp(op, timezone.utc).strftime('%m-%d %H:%M UTC')
            status = f"closed: ${pnl:.2f} ({reason})" if cl else "OPEN"
            print(f"    [{op_str}] entry={ep:.8f} | {status}")
    else:
        print(f"  Paper trades: NONE")

conn.close()
print("\n=== Screen thresholds for reference ===")
print("  Screen A (Momentum):    mcap $5M-$150M, liq $300K+, vol24h $500K+, age >12h")
print("  Screen B (Whale Target): mcap $1M-$200M, liq $200K+, vol24h $250K+, age any")
print("  Screen C (Lottery):     mcap $200K-$5M,  liq $100K+, vol24h $100K+, age >1h")
