from pathlib import Path

content = """# CRITICAL BUGS FOUND 2026-05-21 - Fix Before Anything Else

## Priority 0 - CATASTROPHIC (fix first, these invalidate all data)

### BUG A: Position sizing exceeds available capital
- Paper trader opened 10 simultaneous WCOR positions at ~\$18.50 each = \$185 on a \$100 account
- Dashboard shows -217% equity and 201% drawdown, which is mathematically impossible in spot
- ROOT CAUSE: paper_trader has no check for 'do we have enough capital before opening'
- FIX: track available_capital, refuse to open if position_size > available, cap total exposure
- This single bug makes all P&L numbers meaningless

### BUG B: Same-token mass re-entry (cooldown not preventing it)
- 10 WCOR entries, 8 HYPE signals, 8 TRX signals in one window
- Cooldown rule was supposed to block re-entry for 24h after stop-out
- HYPOTHESIS: all entries fire in the same cycle BEFORE any position closes, so no
  stop-out is recorded yet to trigger the cooldown. Cooldown only works AFTER a close.
- FIX: also block re-entry if an OPEN position already exists for that token
  (paper_trader has SKIP logic for this, but signals still generate and may
  open before the skip - need to verify the skip actually works per-cycle)

### BUG C: Stop loss did not protect against WCOR crash
- WCOR stops set ~-10%, actual exits at -93%
- WCOR appears to have rugged/crashed inside a single 15-min monitor window
- The 'stop' recorded the price at next check, not the stop level
- This is partly inherent to paper trading (can't fill between checks) but in LIVE
  trading a 93% gap-down would also blow through the stop - this is real risk
- FIX OPTIONS:
  1. Add liquidity-crash detection (if price drops >X% in one candle, flag as rug)
  2. Pre-trade rug check tightening (WCOR passed rug check but then rugged)
  3. Hard cap on max loss per position (close to USDC immediately if down >15%)

## Priority 1 - Strategy has no edge (the original problem, still unsolved)
- Win rate 32%, expectancy -\$1.96/trade even setting aside WCOR
- Momentum strategy: 31% win rate across 83 trades
- Avg win +\$1.10, avg loss -\$3.42 (losses 3x bigger than wins)
- Even excluding WCOR, the win/loss ratio is upside down
- Entry logic finds tokens near tops, not in healthy uptrends (unconfirmed theory)

## Priority 2 - Improvements (only after P0 and P1 fixed)
- Faster/tighter exits (trailing stops)
- Entry logic redesign (pullback entries vs continuation breakouts)
- Sentiment data integration (LunarCrush API tier ~\$240/mo - only if strategy validates)

## Observations that are actually GOOD
- HYPE momentum trades: many small wins (+3-6% each), consistent
- TSUKI lottery: small consistent wins (+3% each)
- TRX: tiny but positive (+0.5% each) - though these barely cover fees
- The strategy CAN win on some tokens - the problem is catastrophic losses on others
  destroy all the small wins. This is a risk management failure more than a
  signal-generation failure.

Last updated: 2026-05-21
"""

Path('CRITICAL_BUGS.md').write_text(content, encoding='utf-8')
src = Path('CRITICAL_BUGS.md').read_text(encoding='utf-8').replace(chr(92)+'$', '$')
Path('CRITICAL_BUGS.md').write_text(src, encoding='utf-8')
print('Wrote CRITICAL_BUGS.md')
