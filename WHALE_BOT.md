# Whale Trace Bot — Realistic Copy-Trading Simulation

Follows the top realized-PnL wallets on Solana (from Birdeye's 7-day AND
30-day trader leaderboards — the same data behind Phantom's Explore > Top
Traders) and simulates copying their trades at prices a real copier could
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
- Watchlist merges the 7d and 30d leaderboards (30d stats scaled to weekly
  rate so both windows compete on the same bar), keeps the top 10
- **Farming-ring screen (2026-07-26):** overlap analysis of top-trader buys
  exposed two coordinated rings — 4 wallets that co-bought 15 fresh clone
  mints (ten named "VORF"), and 2 wallets that co-bought two mints both named
  "USOX". Their leaderboard PnL is manufactured by pumping their own launches.
  All 6 are culled permanently. Lesson: tokens "in common" among leaderboard
  wallets are usually a wash-trading signature, not shared conviction.

## Money management (all shadow strategies)

- **2x rule** — when a position's value doubles, the bot sells enough to
  recover the full initial stake; the remaining tokens ride risk-free
  ("initial out" Telegram alert). The position can no longer lose money.
- **Break-even stop** — if a position was up 30%+ and falls back to flat, it
  closes at break-even instead of turning into a loser.
- **Hard stops** — -50% (whale trace, fresh listings), -30% (breakouts).
- **Gap risk caveat** — with 15-min polling, a token that rugs between checks
  is recorded at the real (worse) price. No stop can prevent that; only
  position sizing can.

Legacy fill-price shadows (the "+$3.5M" era) are kept in the DB as
`entry_mode IS NULL` but excluded from all stats.

## Token safety gate (`scripts/token_safety.py`)

Every shadow entry — whale trace, fresh_listing, breakout, bb_bounce — runs a
full two-layer token audit before opening. On Solana there is no per-token
contract bytecode to audit (tokens are instances of the standard SPL program,
unlike EVM chains where tools like SolidityScan scan Solidity source), so the
audit covers the three places rugs actually live: mint configuration, holder
distribution, and liquidity pool status.

**Layer 1 — mint configuration audit (Birdeye `token_security`):**

Hard blocks (never bought, regardless of score):
- Non-transferable token — can buy, can never sell (pure honeypot)
- Freeze authority active — dev can freeze your wallet after purchase
- Transfer fee > 5% (token-2022 tax honeypot)
- Birdeye fake-token / impersonation flag

Scored risks:
- Mint authority active (+40) — dev can print unlimited supply
- Creator still holds >30% of supply (+35, slow-rug) or >5% (+15)
- Top-10 holder concentration excluding LPs (+10 to +30)
- Transfer fee 1-5% (+15), mutable metadata (+10), opaque token-2022 (+10)
- Jupiter strict-list membership (-15, externally vetted)

**Layer 2 — liquidity & insider audit (RugCheck.xyz public API):**
- Token already flagged as rugged → hard block
- Danger-level findings (+15 each, cap +45): LP unlocked, low liquidity,
  single-holder dominance. Scored rather than hard-blocked because every
  minutes-old launch trips these — the per-strategy cap decides.
- Warn-level findings (+5 each, cap +15), e.g. few LP providers
- Insider wallet network from their transaction-graph analysis (+10)
- Best-effort: if RugCheck is down the gate runs on Layer 1 alone
  (Birdeye down = block; no data, no trade)

**Layer 3 (optional) — Token Sniffer (paid API):**
Activates automatically if `TOKENSNIFFER_API_KEY` is added to `.env`
(their API is paid; the free website is CAPTCHA-gated and can't be
automated). Unique adds over the free layers: a similarity database of
known scam contracts (hard block on match) and a direct sell simulation
(hard block if a sell fails — definitive honeypot proof). Low Token
Sniffer safety scores add +15/+30 to our risk score.

Caps: 50 for breakout/bb_bounce/whale trace, 80 for fresh listings. Results
cache for 6h in the `token_safety` table. Manual audit of any token:
`python scripts/token_safety.py <mint_address>`.

What this still can't catch: coordinated multi-wallet dumps by unlinked
wallets and social-engineering rugs. No pre-buy scanner can.

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
- **bb_bounce** — reviews the charts of the top trending (highest 1h volume)
  tokens every cycle. Entry requires ALL of:
  1. A candle tagged the lower Bollinger Band and the latest candle is green
  2. MACD histogram turning green
  3. Stoch RSI bottomed (<25 in last 4 bars) and rising toward the midline
  4. Momentum (MOM-10) trending up
  5. Transaction review: last 50 swaps must be ≥55% buys by volume
  A roadmap event within 14 days (edit `data/roadmap_events.json`) counts as
  one confirmation. Detected chart patterns (double bottom, higher lows) are
  recorded on each entry so results can be broken down by setup.
  -25% stop, 48h max hold. Sentiment analysis requires a paid X/LunarCrush
  API key — the entry hook exists if one is added later.

Reality check: true same-block sniping is won by MEV bots on dedicated RPC
infrastructure — no polling bot can compete there. These strategies test the
edges reachable at 15-minute detection speed, and the shadow stats will show
whether they exist. Early evidence: fresh tokens can drop 98% between polls.

## LIVE trading (`scripts/live_trader.py`) — REAL MONEY

Authorized 2026-07-27: **$100 lifetime budget**, deployed as four $25
bullets, max 2 concurrent positions, minimum 6h between buys, and a
**drawdown halt** — if realized live losses reach -$50, all new buying stops
permanently until the user intervenes. Paper/shadow trading continues in
parallel regardless. The executor runs every 15 min (TradingBotLive) and
only buys when ALL of these hold:

- **Signal**: 2+ distinct traced whales bought the same token within 90 min
  (whale confluence), or a bb_bounce full-checklist entry just fired
- **Safety**: fresh audit score <= 30 (shadow gate allows 50), no hard blocks
- **Liquidity**: >= $100k
- **Wallet funded**: needs trade size + 0.01 SOL fee buffer; if underfunded
  it stays armed and pings Telegram with the missed candidate

Execution is via Jupiter (quote -> swap -> sign locally -> send; the key
never leaves the machine, 3% max slippage). Exits are automated: recover the
initial stake at 2x, -35% hard stop, break-even stop after +30%, 48h max
hold. Every buy/derisk/close is an audible Telegram alert. State lives in
the `live_trades` table; once $25 is spent, it manages exits only.

Check anytime: `python scripts/live_trader.py --status`

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

## Robinhood Chain live trader (`scripts/rh_live_trader.py`)

Solana Jupiter live trading is **off**. Real-money execution for ETH on
Robinhood Chain (chain ID 4663) lives in a separate executor that uses the
0x Swap API **AllowanceHolder** path — never Jupiter, never Permit2, and
never an approval to the 0x Settler contract.

**Wallet:** `0x4D772dB54461eAc90538d6c1E336841f4ACbD91c` (must match the
key in `EVM_PRIVATE_KEY`). Native gas token is ETH.

**Spend is armed in git** (`RH_LIVE_ENABLED = True`) after the 2026-09-12
plumbing check (quote + sign, not broadcast). This cloud host does **not**
run a live cycle. The first real swap happens on your Windows PC after you
pull this branch and register `TradingBotRhLive`.

Windows (from `C:\Users\seanf\Documents\trading-bot`):

```
git fetch origin
git checkout tests-paper-trader
git pull origin tests-paper-trader
.venv\Scripts\pip.exe install -r requirements.txt
.venv\Scripts\python.exe scripts\rh_live_trader.py --status
.venv\Scripts\python.exe scripts\rh_live_trader.py --test-plumbing
powershell -ExecutionPolicy Bypass -File scripts\setup_whale_tasks.ps1
```

`.env` on that PC must include `EVM_PRIVATE_KEY`, `ZERO_EX_API_KEY`, and
(for fill alerts) `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`. Never paste the
key in chat. `--status` / `--test-plumbing` never broadcast; the scheduled
task (or a bare `python scripts/rh_live_trader.py`) will.

Pilot caps (all of the $50 can still be lost):

| Setting | Default |
|---|---|
| `RH_LIVE_ENABLED` | `True` |
| `RH_BUDGET_USD` | $50 lifetime |
| `RH_TRADE_USD` | $5 |
| `RH_MAX_OPEN` | 1 |
| `RH_MAX_REALIZED_LOSS_USD` | $10 halt |
| `RH_SLIPPAGE_BPS` | 100 (1%) |
| `RH_MAX_ROUNDTRIP_COST_PCT` | 3% 0x buy+sell haircut |
| `RH_MIN_LIQUIDITY` | $100,000 Dexscreener |
| `RH_MIN_VOLUME_1H` | $10,000 |

Entries skip ticker-squat USDG, tokenized-equity symbols, and native/WETH.
Optional allow-list: `data/rh_watchlist.json`. US stock tokens are out of
universe. Put `EVM_PRIVATE_KEY` and `ZERO_EX_API_KEY` in `.env` yourself —
never in chat.
