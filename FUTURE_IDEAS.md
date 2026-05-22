# Future Improvements - Reviewed at 50+ Trades

This file tracks ideas for improving the strategy that came up during
development but were deliberately deferred until we have enough trade
data to evaluate them objectively.

DO NOT implement any of these until the go_live_dashboard shows 50+ closed
trades. With fewer trades, we're tuning on noise, not signal.

## Pending Ideas

### Same-token cooldown after stop-out
**Source:** ASTEROID lost 3 times in 24 hours (May 13, 2026)
**Rule:** After a STOP_LOSS exit on token X, do not re-enter token X for 24h
**Hypothesis:** Prevents getting caught in the same downtrend repeatedly
**Counter-risk:** May miss legitimate re-entry setups during fast recoveries
**How to evaluate at 50 trades:**
  - Count: how many "same token, lost again within 24h" occurrences in full history?
  - If 5+ occurrences with this pattern -> rule probably helps
  - If <3 occurrences -> ASTEROID was an outlier, don't add the rule
**Implementation sketch (when ready):**
  In strategy.py before evaluating a token:
    last_stop = SELECT MAX(closed_at) FROM paper_trades
                WHERE address = ? AND close_reason LIKE 'STOP_LOSS%'
    if last_stop and (now - last_stop) < 86400:
        skip this token, log reason

### Tighter stops on high-volatility tokens
**Source:** ASTEROID losses were 15-18%, not the planned 10%
**Hypothesis:** Tokens with ATR > X% of price need tighter stops
**Counter-risk:** Tighter stops = more stop-outs on winners that would have recovered
**How to evaluate at 50 trades:**
  - Plot ATR/price ratio vs realized loss size
  - If correlation > 0.6 -> volatility-adjusted stops worth trying
**Implementation sketch:** scale ATR_STOP_MULTIPLIER by token volatility tier

### Strategy-level win rate divergence
**Source:** whale_copy at 71% win rate (7 trades) vs momentum at 45% (11 trades)
**Hypothesis:** whale_copy may have inherent edge over momentum on memecoins
**Counter-risk:** 7 trades is far too few to draw conclusions; could be variance
**How to evaluate at 50 trades:**
  - If whale_copy still > momentum after 20+ trades each, increase whale_copy allocation
  - If they converge -> keep current 70/20/10 split

### Per-token win/loss history weighting
**Source:** Some tokens (BULL) seem to work well, others (ASTEROID) struggle
**Hypothesis:** Tokens that have won recently are likely to win again
**Counter-risk:** Classic outcome bias - past trades don't predict future setups
**How to evaluate at 50 trades:**
  - Look at win rate per token (need 5+ trades per token)
  - If win rate by token is consistent across time -> some signal
  - If random walk -> no signal, don't add this

### Slippage measurement
**Source:** Paper trades assume zero slippage, live trades will have real slippage
**How to evaluate:** Once we have 10+ live trades, compare paper P&L vs live P&L
**Action:** If live consistently underperforms paper by >2%, adjust strategy thresholds

## Implemented Already (do not re-add)

- Dedupe: one open position per token at a time (paper_trader.py)
- Manual confirmation per live trade (execution_coordinator.py, first 2 weeks)
- Daily spend cap (execution_coordinator.py, $30/day initial)
- Telegram alerts only on actual position opens, not signal re-fires
- 15-minute monitor cadence (max useful given 15m candle intervals)

## How to Use This File

When go_live_dashboard.py shows 'Minimum trades' passed:
  1. Open this file
  2. For each pending idea, run the evaluation criteria
  3. If criteria met: discuss implementation
  4. If not met: leave deferred, re-check after another 30 trades

Reviewing prematurely (before 50 trades) is how strategies get over-tuned
into worse performance than they started with.

Last updated: 2026-05-14
