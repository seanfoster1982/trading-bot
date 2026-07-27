"""Whale Copy Bot — production configuration.

Single source of truth for the finished Solana whale-copy product.
60 closed paper trades showed whale_copy at 57% win rate (+$2.51) while
momentum lost -$7.80 at 24% — this config disables the losing strategies
and concentrates capital on whale_target screened tokens only.

Set WHALE_ONLY = False to revert to the legacy 3-strategy mode.
"""
from __future__ import annotations

# --- Mode ---
WHALE_ONLY = True
ACTIVE_SCREEN = "whale_target"  # screened_tokens.screen value for whale copy

# --- Scheduling (Windows task uses this for documentation; task is 60 min) ---
PIPELINE_INTERVAL_MINUTES = 60

# --- Birdeye budget savers ---
# How many Screen B tokens get the Top Traders API pass per cycle.
WHALE_TOP_TRADER_DEPTH = 10

# Max tokens per screen query (lower = fewer CUs).
SCREEN_TOKEN_LIMIT = 30

# --- Capital (paper / live sizing) ---
TOTAL_CAPITAL_USD = 100.0

# Only whale_copy is active in WHALE_ONLY mode.
ALLOCATION = {
    "whale_copy": {
        "pct": 1.0,
        "max_positions": 4,
        "per_position_usd": 25.0,
    },
    # Legacy buckets — disabled (kept so old DB rows / tests don't KeyError).
    "momentum": {"pct": 0.0, "max_positions": 0, "per_position_usd": 0.0},
    "lottery": {"pct": 0.0, "max_positions": 0, "per_position_usd": 0.0},
}

# Extra CLI args passed to screen_memecoins.py from the monitor loop.
SCREENER_ARGS = ["--whale-only", f"--whale-depth={WHALE_TOP_TRADER_DEPTH}"]

# --- Whale Trace (separate analysis — shadow-follows specific high-PnL wallets) ---
# Writes ONLY to whale_wallets / whale_trace_trades tables. Never touches
# signals or paper_trades, so it cannot contaminate the whale_copy strategy.
WHALE_TRACE_ENABLED = True
WHALE_TRACE_MAX_WALLETS = 10        # wallets actively traced per cycle
WHALE_TRACE_MIN_TRADES_1W = 20      # leaderboard filter: skip 1-lucky-trade wallets
WHALE_TRACE_MIN_REALIZED_PNL = 1000.0   # $ realized (not paper) profit last week
WHALE_TRACE_MIN_BUY_USD = 500.0     # ignore wallet buys smaller than this
WHALE_TRACE_SHADOW_SIZE_USD = 25.0  # hypothetical $ per shadow position
WHALE_TRACE_MAX_HOLD_HOURS = 24     # force-close shadow position after this
WHALE_TRACE_LEADERBOARD_REFRESH_HOURS = 24

# Realistic-copy simulation ("market" mode) — entries/exits at the price WE
# could get when the whale's trade is detected, not the whale's own fill.
WHALE_TRACE_POLL_MINUTES = 15       # scheduled task interval (docs)
WHALE_TRACE_SLIPPAGE_PCT = 2.0      # paid on entry and exit
WHALE_TRACE_STOP_LOSS_PCT = 50.0    # close shadow if down this much
WHALE_TRACE_MAX_CHASE_MULT = 2.0    # skip entry if market already 2x whale's fill
WHALE_TRACE_CULL_MIN_CLOSED = 5     # closed shadows before a wallet can be culled

# --- Shadow money management (applies to Whale Trace AND Market Sniper) ---
# When a shadow position doubles, "sell" enough to recover the initial stake;
# the remainder rides risk-free. Before that, if a position was up more than
# the trigger and falls back to flat, close at break-even instead of letting
# a winner become a loser. Hard stop-losses below remain the base protection.
SHADOW_TAKE_INITIAL_MULT = 2.0      # take initial out at 2x
SHADOW_BREAK_EVEN_TRIGGER_PCT = 30.0

# --- Market Sniper (separate shadow strategies + market-wide pulse) ---
# Same honest accounting as Whale Trace: entries at detection-time price with
# slippage, own tables only, never touches real/paper trading.
SNIPER_ENABLED = True
SNIPER_SHADOW_SIZE_USD = 25.0
SNIPER_SLIPPAGE_PCT = 3.0           # fresh tokens are thin; assume worse fills
SNIPER_REENTRY_COOLDOWN_HOURS = 24  # don't re-buy a token right after closing it
SNIPER_MAX_NEW_PER_CYCLE = 3        # per strategy, avoids flooding on hot markets

# Strategy A: fresh listings — tokens minutes old with real liquidity.
# Exits: take-initial at 2x, break-even stop, hard stop, max hold.
SNIPER_FRESH = {
    "min_liquidity": 10_000.0,
    "max_age_minutes": 45,
    "stop_pct": 50.0,
    "max_hold_hours": 12,
    "max_open": 8,
}

# Strategy B: breakouts — established-enough tokens accelerating right now.
# The +25%..+300% band deliberately excludes launch-pump garbage (+60,000%).
SNIPER_BREAKOUT = {
    "min_liquidity": 75_000.0,
    "min_volume_1h": 50_000.0,
    "min_change_1h": 25.0,
    "max_change_1h": 300.0,
    "stop_pct": 30.0,
    "max_hold_hours": 24,
    "max_open": 8,
}
