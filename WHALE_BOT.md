# Whale Copy Bot — Finished Product

Solana memecoin bot focused on **whale_target** tokens only. Paper trading
showed **57% win rate** on whale_copy vs **24%** on momentum over 60 trades.

## What it does

1. **Screens** Birdeye for tokens where smart money is active (Screen B only).
2. **Checks** rug score, whale concentration, macro regime.
3. **Enters** on 15m technical triggers (MACD cross, Stoch RSI, BB bounce) with
   5m VWAP confirmation — same proven entry logic, but only on whale-picked tokens.
4. **Paper-trades** (or live via execution_coordinator) with ATR stops and 2R targets.
5. **Telegrams** you on every BUY/close plus 9am/9pm portfolio digests.
6. **Whale Trace** (separate analysis): shadow-follows the top-PnL wallets from
   Birdeye's weekly leaderboard. When a traced wallet buys a token (≥$500), a
   hypothetical $25 "shadow" position is logged at their fill price and closed
   when they sell (or after 24h). Results appear in the digest under
   "Whale Trace" — these are NOT real or paper trades and never touch the
   whale_copy strategy. Run `python scripts/whale_trace.py --report` anytime.

## Quick start (Windows)

```powershell
cd C:\Users\seanf\Documents\trading-bot

# 1. Ensure .env has BIRDEYE_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# 2. Install deps (once)
.\.venv\Scripts\pip install -r requirements.txt

# 3. Register scheduled tasks (60-min pipeline + 2x daily Telegram digest)
powershell -ExecutionPolicy Bypass -File scripts\setup_whale_tasks.ps1

# 4. Run one cycle now (optional — tasks also fire automatically)
.\.venv\Scripts\python.exe scripts\monitor_loop.py --once

# 5. Check status anytime
.\.venv\Scripts\python.exe scripts\bot_report.py --send
.\.venv\Scripts\python.exe check_clean.py
```

## Manage tasks

```powershell
# Pause trading (stops Birdeye usage)
schtasks /Change /TN "TradingBotMonitor" /DISABLE

# Resume
schtasks /Change /TN "TradingBotMonitor" /ENABLE
schtasks /Run /TN "TradingBotMonitor"

# Remove everything
schtasks /Delete /TN "TradingBotMonitor" /F
schtasks /Delete /TN "TradingBotDigest" /F
```

## Configuration

Edit `scripts/whale_config.py`:

| Setting | Default | Purpose |
|---|---|---|
| `WHALE_ONLY` | `True` | Disable momentum + lottery |
| `PIPELINE_INTERVAL_MINUTES` | `60` | How often to run (task must match) |
| `WHALE_TOP_TRADER_DEPTH` | `10` | Top-trader API calls per cycle |
| `max_positions` | `4` | Max open whale_copy positions |
| `per_position_usd` | `$25` | Size per trade |
| `WHALE_TRACE_ENABLED` | `True` | Shadow-follow leaderboard wallets |
| `WHALE_TRACE_MIN_TRADES_1W` | `20` | Skip lucky one-trade wallets |
| `WHALE_TRACE_MIN_REALIZED_PNL` | `$1000` | Require realized (not paper) profit |
| `WHALE_TRACE_MIN_BUY_USD` | `$500` | Ignore small wallet buys |

## Go live (real money)

**Do not skip paper validation.** Current paper stats: +$2.51 whale_copy on 14
trades, account net -$4.83 only because momentum was enabled historically.

1. Paper trade whale-only for 2+ more weeks with positive expectancy.
2. Configure wallet keys in `.env` (see `.env.example`).
3. Run `python scripts/smoke_test.py` — all checks green.
4. Run `python scripts/execution_coordinator.py` (reads signals, executes via Jupiter).
5. Start with **$25–50** total capital, not full size.

## Honest limits

- **Not literal wallet mirroring yet.** whale_copy = tokens where Birdeye Top
  Traders are active, plus technical entry. True wallet-copy is scaffolded in
  `strategies/solana/copy_trade.py` for a future phase.
- **Polymarket / Kalshi / arbitrage** live in the separate `strategies/` +
  `exchanges/` framework — not wired to this Solana pipeline yet.
- **Past performance ≠ future results.** 57% on 14 trades is promising, not proof.

## Logs

- `data/overnight.log` — one line per pipeline cycle
- `data/alerts.log` — BUY signals and position closes
- Telegram — real-time pushes when configured
