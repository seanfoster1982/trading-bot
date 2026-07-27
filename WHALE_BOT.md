# Whale Trace Bot — Realistic Copy-Trading Simulation

Follows the top realized-PnL wallets on Solana (from Birdeye's weekly trader
leaderboard) and simulates copying their trades at prices a real copier could
actually get. Also runs **Market Sniper** — see below.

## Why the pivot

Paper results over 77 closed trades:

| Strategy | Trades | Win rate | P&L | Status |
|---|---|---|---|---|
| whale_copy | 31 | 42% | -$12.20 | retired |
| lottery | 21 | 48% | +$0.46 | retired |
| momentum | 25 | 24% | -$7.80 | retired |

The technical-entry strategies didn't hold an edge. Whale Trace replaces them:
instead of screening tokens, it follows *wallets* with proven realized profit.

**Important:** raw leaderboard PnL is mostly uncopyable (launch snipers fill at
prices that exist for milliseconds). Whale Trace measures what YOU would make:

- Entry at the market price when we detect their buy (15-min polling), +2% slippage
- Skips entries where the token already ran 2x past the whale's fill
- Skips round-trips completed before we could have reacted
- Exit when the whale sells (at our detection price), at -50% stop, or 24h max hold
- Wallets with 5+ closed shadows and negative copyable P&L get culled permanently

Legacy fill-price shadows (the "+$3.5M" era) are kept in the DB as
`entry_mode IS NULL` but excluded from all stats.

## Market Sniper (`scripts/market_sniper.py`)

Two additional shadow strategies plus market-wide analysis, same honest
accounting (detection-time prices, 3% slippage each way, own tables only):

- **fresh_listing** — tokens < 45 min old that already attracted ≥$10k
  liquidity. Lottery profile: -50% stop, +100% target, 12h max hold.
- **breakout** — tokens with ≥$75k liquidity and ≥$50k 1h volume moving
  +25%..+300% in the last hour. The band excludes launch-pump spikes
  (+60,000%) that are already over. -30% stop, +60% target, 24h max hold.
- **market pulse** — every cycle logs breadth of the top-100 volume tokens
  (median 1h change, % gainers) and the new-listing rate; shown in the digest.

Reality check: true same-block sniping is won by MEV bots on dedicated RPC
infrastructure — no polling bot can compete there. These strategies test the
edges reachable at 15-minute detection speed, and the shadow stats will show
whether they exist. Early evidence: fresh tokens can drop 98% between polls.

## Operation

Three scheduled tasks (register with `scripts\setup_whale_tasks.ps1`):

- **TradingBotWhaleTrace** — every 15 min: poll wallets, open/close shadows,
  Telegram push on each open (silent) and close (audible)
- **TradingBotSniper** — every 15 min: fresh-listing + breakout scans, pulse
- **TradingBotDigest** — 9am/9pm: portfolio + whale trace + sniper digest

The old TradingBotMonitor pipeline (screener/ingest/indicators/strategy) is
retired and its task deleted. `monitor_loop.py` still exists if you ever want
the legacy strategies back.

```powershell
# Status anytime
.\.venv\Scripts\python.exe scripts\whale_trace.py --report

# Pause / resume
schtasks /Change /TN "TradingBotWhaleTrace" /DISABLE
schtasks /Change /TN "TradingBotWhaleTrace" /ENABLE

# Remove everything
schtasks /Delete /TN "TradingBotWhaleTrace" /F
schtasks /Delete /TN "TradingBotDigest" /F
```

## Configuration (`scripts/whale_config.py`)

| Setting | Default | Purpose |
|---|---|---|
| `WHALE_TRACE_MAX_WALLETS` | `10` | Wallets traced concurrently |
| `WHALE_TRACE_MIN_TRADES_1W` | `20` | Skip lucky one-trade wallets |
| `WHALE_TRACE_MIN_REALIZED_PNL` | `$1000` | Require realized (not paper) profit |
| `WHALE_TRACE_MIN_BUY_USD` | `$500` | Ignore small wallet buys |
| `WHALE_TRACE_SLIPPAGE_PCT` | `2%` | Charged on entry AND exit |
| `WHALE_TRACE_STOP_LOSS_PCT` | `50%` | Hard stop on shadows |
| `WHALE_TRACE_MAX_CHASE_MULT` | `2.0` | Don't buy if already 2x whale's fill |
| `WHALE_TRACE_CULL_MIN_CLOSED` | `5` | Closed shadows before culling |

## Go-live criteria

Do NOT put real money on this until the **market-mode** stats show, over at
least 2 weeks and 30+ closed shadows:

1. Positive total P&L after slippage
2. At least 2-3 surviving (unculled) wallets producing the profit
3. Win rate and average P&L per trade you'd accept live

Shadow P&L at 15-min polling is still optimistic vs. live execution (real
fills, MEV, failed txs). Treat it as an upper bound.

## Logs

- `whale_trace_trades` table in `data/memecoins.db` — every shadow trade
- `whale_wallets` table — watchlist with culled flags
- Telegram — real-time shadow opens/closes + digests
