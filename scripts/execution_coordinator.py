"""Execution coordinator - Piece 6 of the execution adapter.

Reads pending BUY signals, executes them as live trades via Pieces 3+4+5.
Enforces safety limits:
  - Manual 'yes' confirmation per trade (first 2 weeks)
  - $30/day hard spend cap (first 2 weeks)
  - One open live position per token (dedupe)
  - Only signals generated AFTER coordinator start time
  - Skips signals where the strategy entry price is too far from current quote

Tracks all live trades in the live_trades table.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.wallet_loader import verify_wallet
from scripts.jupiter_swap_builder import fetch_quote, build_swap_template
from scripts.sign_submit import sign_and_submit, SubmissionStatus
from scripts.token_metadata import get_decimals

load_dotenv(dotenv_path=Path('.env'))

# === SAFETY LIMITS ===
DAILY_SPEND_CAP_USD = 30.0        # First 2 weeks. Bump to 70 after validation.
MAX_TRADES_PER_DAY = 10           # Circuit breaker on trade frequency
ENTRY_PRICE_DRIFT_PCT = 5.0       # Skip signal if fill price > 5% from strategy price
ENTRY_SLIPPAGE_BPS = 300          # 3% slippage tolerance on entries
EXIT_SLIPPAGE_BPS = 500           # 5% slippage tolerance on exits (better to exit at bad price than not exit)

USDC_MINT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
USDC_DECIMALS = 6
DB_PATH = Path('data/memecoins.db')


def init_live_trades_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER,
            symbol TEXT NOT NULL,
            address TEXT NOT NULL,
            side TEXT NOT NULL,
            strategy TEXT,
            opened_at INTEGER,
            entry_signature TEXT,
            entry_usdc_spent REAL,
            entry_tokens_received REAL,
            entry_fill_price REAL,
            strategy_entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            closed_at INTEGER,
            close_signature TEXT,
            close_tokens_sold REAL,
            close_usdc_received REAL,
            close_fill_price REAL,
            close_reason TEXT,
            pnl_usd REAL,
            pnl_pct REAL,
            status TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_live_trades_open
        ON live_trades(closed_at)
    """)
    conn.commit()
    conn.close()


def get_today_utc_start() -> int:
    """Unix timestamp at start of current UTC day."""
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def get_daily_spend_so_far() -> tuple[float, int]:
    """Returns (usd_spent_today, trades_opened_today)."""
    day_start = get_today_utc_start()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        '''SELECT COALESCE(SUM(entry_usdc_spent), 0), COUNT(*)
           FROM live_trades
           WHERE opened_at >= ?''',
        (day_start,)
    ).fetchone()
    conn.close()
    return float(row[0] or 0), int(row[1] or 0)


def get_open_live_addresses() -> set[str]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT address FROM live_trades WHERE closed_at IS NULL"
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def get_new_buy_signals_since(since_ts: int) -> list[dict]:
    """BUY signals generated after coordinator start, not already executed live."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT s.id, s.symbol, s.address, s.strategy,
               s.entry_price, s.stop_loss, s.take_profit, s.position_size_usd,
               s.generated_at
        FROM signals s
        WHERE s.action = 'BUY'
          AND s.generated_at >= ?
          AND NOT EXISTS (
              SELECT 1 FROM live_trades lt WHERE lt.signal_id = s.id
          )
        ORDER BY s.generated_at ASC
    """, (since_ts,)).fetchall()
    conn.close()
    cols = ['id', 'symbol', 'address', 'strategy', 'entry_price',
            'stop_loss', 'take_profit', 'position_size_usd', 'generated_at']
    return [dict(zip(cols, r)) for r in rows]


def get_open_live_positions() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT id, symbol, address, strategy, opened_at,
               entry_tokens_received, entry_fill_price,
               stop_loss, take_profit
        FROM live_trades
        WHERE closed_at IS NULL
    """).fetchall()
    conn.close()
    cols = ['id', 'symbol', 'address', 'strategy', 'opened_at',
            'tokens_held', 'entry_fill_price', 'stop_loss', 'take_profit']
    return [dict(zip(cols, r)) for r in rows]


def get_latest_5m_close(address: str) -> Optional[float]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT close FROM indicators
        WHERE address = ? AND interval = '5m'
        ORDER BY timestamp DESC LIMIT 1
    """, (address,)).fetchone()
    conn.close()
    return row[0] if row else None


def record_live_open(sig, signature, usdc_spent, tokens_received, fill_price):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO live_trades
        (signal_id, symbol, address, side, strategy, opened_at,
         entry_signature, entry_usdc_spent, entry_tokens_received,
         entry_fill_price, strategy_entry_price, stop_loss, take_profit, status)
        VALUES (?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
    """, (
        sig['id'], sig['symbol'], sig['address'], sig['strategy'],
        int(time.time()), signature, usdc_spent, tokens_received,
        fill_price, sig['entry_price'], sig['stop_loss'], sig['take_profit'],
    ))
    conn.commit()
    conn.close()


def record_live_close(trade_id, signature, tokens_sold, usdc_received,
                      fill_price, close_reason, pnl_usd, pnl_pct):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE live_trades
        SET closed_at = ?, close_signature = ?,
            close_tokens_sold = ?, close_usdc_received = ?,
            close_fill_price = ?, close_reason = ?,
            pnl_usd = ?, pnl_pct = ?, status = 'CLOSED'
        WHERE id = ?
    """, (int(time.time()), signature, tokens_sold, usdc_received,
           fill_price, close_reason, pnl_usd, pnl_pct, trade_id))
    conn.commit()
    conn.close()


def usd_to_atomic_usdc(usd: float) -> int:
    return int(usd * (10 ** USDC_DECIMALS))


def confirm_or_abort(prompt: str) -> bool:
    """Ask user for 'yes' confirmation. Returns True if confirmed."""
    resp = input(prompt + ' [yes/N]: ').strip().lower()
    return resp == 'yes'


async def execute_buy(client, kp, sig, wallet) -> bool:
    """Execute a single BUY signal. Returns True if trade opened."""
    symbol = sig['symbol']
    print()
    print('-' * 70)
    print(f"NEW BUY SIGNAL: {symbol}")
    print('-' * 70)
    print(f"Strategy: {sig['strategy']}")
    print(f"Strategy entry price: {sig['entry_price']:.8f}")
    print(f"Stop loss: {sig['stop_loss']:.8f}")
    print(f"Take profit: {sig['take_profit']:.8f}")
    print(f"Position size: ${sig['position_size_usd']:.2f}")

    # Fetch a real quote at the strategy size
    usdc_atomic = usd_to_atomic_usdc(sig['position_size_usd'])
    try:
        quote = await fetch_quote(client, USDC_MINT, sig['address'],
                                    amount_atomic=usdc_atomic,
                                    slippage_bps=ENTRY_SLIPPAGE_BPS)
    except Exception as e:
        print(f"  ABORT: quote failed: {e}")
        return False

    out_decimals = get_decimals(sig['address']) or 6
    tokens_out = int(quote['outAmount']) / (10 ** out_decimals)
    fill_price = sig['position_size_usd'] / tokens_out if tokens_out > 0 else 0
    drift_pct = ((fill_price - sig['entry_price']) / sig['entry_price'] * 100) if sig['entry_price'] else 0
    impact = float(quote.get('priceImpactPct') or 0) * 100

    print()
    print(f"Live quote: ${sig['position_size_usd']:.2f} -> {tokens_out:,.4f} {symbol}")
    print(f"Fill price: {fill_price:.8f}")
    print(f"Drift from strategy: {drift_pct:+.2f}%")
    print(f"Price impact: {impact:.2f}%")
    print(f"Route: {len(quote.get('routePlan', []))} steps")

    # Skip if fill drifted too far
    if abs(drift_pct) > ENTRY_PRICE_DRIFT_PCT:
        print(f"  SKIP: drift {drift_pct:+.2f}% exceeds {ENTRY_PRICE_DRIFT_PCT}% threshold")
        return False

    if not confirm_or_abort(f"Execute live BUY {symbol} for ${sig['position_size_usd']:.2f}?"):
        print("  ABORTED by user.")
        return False

    try:
        template = await build_swap_template(client, quote, wallet)
    except Exception as e:
        print(f"  ABORT: template build failed: {e}")
        return False

    result = await sign_and_submit(client, template, kp)
    print(f"  Result: {result.status.value.upper()}  (elapsed {result.elapsed_seconds:.1f}s)")
    if result.signature:
        print(f"  Signature: {result.signature}")
        print(f"  Solscan: https://solscan.io/tx/{result.signature}")
    if result.error:
        print(f"  Error: {result.error}")

    if result.status == SubmissionStatus.CONFIRMED:
        record_live_open(sig, result.signature,
                          sig['position_size_usd'], tokens_out, fill_price)
        print(f"  RECORDED: live position opened in {symbol}")
        return True
    return False


async def check_exits(client, kp, wallet, positions) -> int:
    """Check open positions for stop/target/time exits. Returns closes count."""
    closed_count = 0
    for pos in positions:
        symbol = pos['symbol']
        current_price = get_latest_5m_close(pos['address'])
        if current_price is None:
            print(f"  {symbol}: no current price, skipping")
            continue

        age_hours = (time.time() - pos['opened_at']) / 3600
        exit_reason = None
        if current_price <= pos['stop_loss']:
            exit_reason = f"STOP_LOSS at {current_price:.8f}"
        elif pos['take_profit'] and current_price >= pos['take_profit']:
            exit_reason = f"TAKE_PROFIT at {current_price:.8f}"
        elif age_hours >= 72:
            exit_reason = f"TIME_STOP {age_hours:.0f}h"

        if not exit_reason:
            unrealized_pct = (current_price - pos['entry_fill_price']) / pos['entry_fill_price'] * 100
            print(f"  {symbol}: hold ({unrealized_pct:+.2f}% unrealized)")
            continue

        print()
        print('-' * 70)
        print(f"EXIT TRIGGER: {symbol} - {exit_reason}")
        print('-' * 70)

        # Get exit quote: tokens -> USDC
        token_decimals = get_decimals(pos['address']) or 6
        tokens_atomic = int(pos['tokens_held'] * (10 ** token_decimals))
        try:
            quote = await fetch_quote(client, pos['address'], USDC_MINT,
                                        amount_atomic=tokens_atomic,
                                        slippage_bps=EXIT_SLIPPAGE_BPS)
        except Exception as e:
            print(f"  ABORT exit: quote failed: {e}")
            continue

        usdc_out = int(quote['outAmount']) / (10 ** USDC_DECIMALS)
        fill_price = usdc_out / pos['tokens_held'] if pos['tokens_held'] > 0 else 0
        print(f"Exit quote: {pos['tokens_held']:,.4f} {symbol} -> ${usdc_out:.2f}")
        print(f"Fill price: {fill_price:.8f}")

        if not confirm_or_abort(f"Execute live SELL {symbol}? Reason: {exit_reason}"):
            print("  ABORTED by user.")
            continue

        try:
            template = await build_swap_template(client, quote, wallet)
        except Exception as e:
            print(f"  ABORT: template build failed: {e}")
            continue

        result = await sign_and_submit(client, template, kp)
        print(f"  Result: {result.status.value.upper()}")
        if result.signature:
            print(f"  Solscan: https://solscan.io/tx/{result.signature}")

        if result.status == SubmissionStatus.CONFIRMED:
            # Compute P&L based on actual fills
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute(
                "SELECT entry_usdc_spent FROM live_trades WHERE id = ?",
                (pos['id'],)
            ).fetchone()
            conn.close()
            entry_cost = float(row[0]) if row else 0
            pnl_usd = usdc_out - entry_cost
            pnl_pct = (pnl_usd / entry_cost * 100) if entry_cost else 0
            record_live_close(pos['id'], result.signature,
                              pos['tokens_held'], usdc_out,
                              fill_price, exit_reason, pnl_usd, pnl_pct)
            print(f"  RECORDED: closed {symbol}  P&L: ${pnl_usd:+.2f} ({pnl_pct:+.2f}%)")
            closed_count += 1

    return closed_count


async def main():
    init_live_trades_db()
    kp = verify_wallet()
    wallet = str(kp.pubkey())
    print()
    print('=' * 70)
    print('LIVE EXECUTION COORDINATOR')
    print('=' * 70)
    print(f"Wallet: {wallet}")
    print(f"Daily cap: ${DAILY_SPEND_CAP_USD:.2f}  Max trades/day: {MAX_TRADES_PER_DAY}")
    print(f"Entry slippage: {ENTRY_SLIPPAGE_BPS} bps  Exit slippage: {EXIT_SLIPPAGE_BPS} bps")
    print(f"Manual confirmation required for every trade.")

    coordinator_start = int(time.time())
    spent_today, trades_today = get_daily_spend_so_far()
    print(f"Already spent today: ${spent_today:.2f} ({trades_today} trades)")
    print(f"Only acting on signals generated AFTER {datetime.fromtimestamp(coordinator_start, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print()

    async with httpx.AsyncClient() as client:
        # === Check exits on any open positions FIRST ===
        open_positions = get_open_live_positions()
        if open_positions:
            print(f"Checking exits for {len(open_positions)} open live position(s)...")
            await check_exits(client, kp, wallet, open_positions)
        else:
            print("No open live positions.")

        # === Process new BUY signals ===
        signals = get_new_buy_signals_since(coordinator_start)
        open_addresses = get_open_live_addresses()

        if not signals:
            print()
            print("No new BUY signals since coordinator start. Coordinator idle.")
            print("Run again later, or run from the monitor loop.")
            return

        print()
        print(f"Found {len(signals)} new BUY signal(s).")

        for sig in signals:
            spent_today, trades_today = get_daily_spend_so_far()
            remaining = DAILY_SPEND_CAP_USD - spent_today

            if trades_today >= MAX_TRADES_PER_DAY:
                print(f"\nDAILY TRADE COUNT CAP HIT ({trades_today}/{MAX_TRADES_PER_DAY}). Stopping.")
                break
            if sig['position_size_usd'] > remaining:
                print(f"\nSKIP {sig['symbol']}: ${sig['position_size_usd']:.2f} > ${remaining:.2f} remaining cap")
                continue
            if sig['address'] in open_addresses:
                print(f"\nSKIP {sig['symbol']}: already have an open live position")
                continue

            opened = await execute_buy(client, kp, sig, wallet)
            if opened:
                open_addresses.add(sig['address'])


if __name__ == '__main__':
    asyncio.run(main())
