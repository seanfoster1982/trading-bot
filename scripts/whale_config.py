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
