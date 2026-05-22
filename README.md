# Trading Bot — Polymarket + Solana

A modular automated trading system covering Polymarket prediction markets and
Solana-based DEX trading via Jupiter (Phantom-compatible wallet).

**Includes:** Streamlit dashboard, backtester, hard-cap risk layer, kill switch,
six strategies (4 Polymarket + 2 Solana) plus a score-based benchmark, and
SQLite persistence for the full paper-trading audit trail.

## Status

**Phase 0 scaffold.** This is a starter skeleton, not a profitable bot. Stub
strategies are explicitly marked as such; only `resolution_arb` and
`score_based` ship with real logic. You build edge into the modules, prove it
in backtest and paper, then enable `LIVE_TRADING=true` per strategy.

**34 tests passing** across risk, backtest, score-based, and persistence.

**Windows users:** see `WINDOWS.md` for PowerShell-specific setup.

## Architecture

```
trading-bot/
├── app.py               # Streamlit dashboard (read-only UI)
├── config/              # Settings, secrets loading, per-env config
├── core/
│   ├── engine.py        # Orchestrator: tick → strategies → risk → execute → persist
│   └── models.py        # Domain types (Order, Signal, Fill, Position, etc.)
├── exchanges/
│   ├── polymarket/      # py-clob-client wrapper, Gamma API client
│   └── solana/          # Jupiter swap, RPC, wallet signing
├── strategies/
│   ├── polymarket/      # resolution_arb, copy_trade, news_event,
│   │                    # cross_platform_arb, score_based (benchmark)
│   └── solana/          # jupiter_arb, copy_trade
├── risk/                # Position limits, kill switch, exposure tracker
├── backtest/            # Historical replay engine
├── data/
│   ├── persistence.py   # SQLAlchemy writes for all run-time data
│   └── schema.py        # 6-table schema (orders, fills, positions,
│                        # equity, alerts, markets)
├── monitoring/          # Discord/Telegram alerts (placeholder)
├── scripts/             # CLI entry points
└── tests/               # 34 unit tests
```

## The four ground rules

1. **Strategies declare a mode.** `paper`, `live`, or `disabled`. New strategies
   start `disabled`. They graduate to `paper` after passing backtest. They
   graduate to `live` only after 4+ weeks of profitable paper trading.

2. **Risk layer is unbypassable.** Every order goes through `risk.check_order()`
   before submission. There is no path around it.

3. **Kill switch always works.** `python scripts/kill.py` flattens all
   positions and disables all strategies. It must work even if the main loop
   is wedged.

4. **Nothing logs secrets.** Private keys and API secrets live in `.env`,
   never in code, never in logs, never in error messages.

## Quickstart

```bash
# 1. Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — fill in PRIVATE_KEY, FUNDER_ADDRESS, RPC URLs, etc.

# 3. Initialize SQLite database (creates trading_bot.db with all 6 tables)
python scripts/init_db.py

# 4. Run unit tests — verify the risk, backtester, persistence, and scoring math
pytest tests/ -v
# All 34 tests should pass

# 5. Smoke test connectivity (read-only — no orders placed)
python scripts/smoke_test.py

# 6. Browse current Polymarket universe
python scripts/scan_markets.py

# 7. Backtest — replay strategy against historical resolved markets
python scripts/backtest.py --strategy resolution_arb --markets 50 --verbose

# 8. Streamlit dashboard
#    Reads paper trades, equity, and open positions from SQLite.
#    Tabs: Markets, Paper Trades, Equity, Strategies, Backtest, Risk
streamlit run app.py

# 9. Paper trade live data — orders/fills/positions auto-persist to SQLite
python scripts/run.py --strategies polymarket_resolution_arb --mode paper

# 10. Kill switch (independent — works even if main loop is wedged)
python scripts/kill.py
```

After step 9, refresh the dashboard's Paper Trades and Equity tabs and you'll
see the trades the engine recorded. No log piping required — persistence is
automatic.

## Strategies

### Polymarket

| Strategy | Edge source | Notes |
|---|---|---|
| `resolution_arb` | Markets at 0.97/0.03 that should be 0.99/0.01 | Boring, capital-intensive, real |
| `copy_trade` | Mirror profitable wallets identified via Dune | Edge = picking the right whales |
| `news_event` | LLM reads wire feed, repositions before crowd | Hardest to make work |
| `cross_platform_arb` | Polymarket vs Kalshi vs sportsbook spreads | US has Kalshi as legal alternative |
| `score_based` | None — heuristic ranker | **Baseline benchmark only.** If a real strategy doesn't beat this in backtest, it has no edge. |

### Solana

| Strategy | Edge source | Notes |
|---|---|---|
| `jupiter_arb` | Stale prices across Solana DEX routes | MEV-heavy; small pairs only |
| `copy_trade` | Mirror Solana whale wallets | Same idea as Polymarket |

## Roadmap

- **Week 1–2:** scaffold + Polymarket data ingestion + risk layer
- **Week 3–4:** `resolution_arb` strategy in backtest, then paper
- **Week 5–6:** `copy_trade` for Polymarket
- **Week 7–8:** `cross_platform_arb` (requires Kalshi + odds API)
- **Week 9+:** `news_event` (hard, do last)
- **Phase 2:** Solana strategies after Polymarket is profitable in paper

## Disclaimer

Automated trading can lose money quickly. This software is provided as-is. You
are responsible for legal compliance in your jurisdiction. Polymarket is
restricted in some US jurisdictions — verify your eligibility before
depositing.
