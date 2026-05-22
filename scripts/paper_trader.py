"""Paper trader - simulates the strategy without executing real trades.

Reads BUY signals from the signals table, opens simulated positions at signal price,
tracks them against the latest candle data, exits them when stops/targets hit, and
records P&L to paper_trades table.

Run this periodically (every 15min in production). It will:
  1. Open positions for any new BUY signals
  2. Update prices for all open positions using latest 5m close
  3. Close positions that hit stop loss, take profit, or time stop
  4. Report current P&L

Position sizing comes from the signal itself (already factored in macro etc.).
No real money moves. No wallet touched. Database only.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = Path("data/memecoins.db")

# Max position lifetime (auto-close after this even if no stop/target hit)
MAX_POSITION_AGE_HOURS = 72


def init_paper_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            address TEXT NOT NULL,
            strategy TEXT NOT NULL,
            opened_at INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            position_size_usd REAL NOT NULL,
            tokens_held REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL,
            closed_at INTEGER,
            close_price REAL,
            close_reason TEXT,
            pnl_usd REAL,
            pnl_pct REAL,
            FOREIGN KEY(signal_id) REFERENCES signals(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_paper_open
        ON paper_trades(closed_at)
    """)
    conn.commit()
    conn.close()


def get_unfilled_buy_signals():
    """Get BUY signals that don\'t have an open paper trade yet."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT s.id, s.symbol, s.address, s.strategy,
               s.entry_price, s.stop_loss, s.take_profit, s.position_size_usd,
               s.generated_at
        FROM signals s
        LEFT JOIN paper_trades p ON p.signal_id = s.id
        WHERE s.action = 'BUY'
          AND p.id IS NULL
        ORDER BY s.generated_at ASC
    """).fetchall()
    conn.close()
    cols = ["id", "symbol", "address", "strategy", "entry_price",
            "stop_loss", "take_profit", "position_size_usd", "generated_at"]
    return [dict(zip(cols, r)) for r in rows]


def get_open_positions():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT id, signal_id, symbol, address, strategy, opened_at,
               entry_price, position_size_usd, tokens_held,
               stop_loss, take_profit
        FROM paper_trades
        WHERE closed_at IS NULL
    """).fetchall()
    conn.close()
    cols = ["id", "signal_id", "symbol", "address", "strategy", "opened_at",
            "entry_price", "position_size_usd", "tokens_held",
            "stop_loss", "take_profit"]
    return [dict(zip(cols, r)) for r in rows]


def get_current_price(address):
    """Latest 5m close price for this token."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT close, timestamp FROM indicators
        WHERE address = ? AND interval = \'5m\'
        ORDER BY timestamp DESC LIMIT 1
    """, (address,)).fetchone()
    conn.close()
    if not row:
        return None, None
    return row[0], row[1]


def get_whale_risk_now(address):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT whale_risk_score FROM whale_risk
        WHERE address = ?
        ORDER BY checked_at DESC LIMIT 1
    """, (address,)).fetchone()
    conn.close()
    return row[0] if row else None


# === RISK MANAGEMENT (added 2026-05-21 after WCOR disaster) ===
START_CAPITAL_USD = 100.0
MAX_TOTAL_EXPOSURE_USD = 100.0   # never deploy more than this across all open positions
MAX_LOSS_PCT_HARD = 0.15         # force-close any position down more than 15% at check time


def get_deployed_capital() -> float:
    """Sum of position_size_usd across all currently-open paper trades."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(SUM(position_size_usd), 0) FROM paper_trades WHERE closed_at IS NULL"
    ).fetchone()
    conn.close()
    return float(row[0]) if row else 0.0


def get_realized_pnl() -> float:
    """Sum of realized P&L across all closed paper trades."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl_usd), 0) FROM paper_trades WHERE closed_at IS NOT NULL"
    ).fetchone()
    conn.close()
    return float(row[0]) if row else 0.0


def available_capital() -> float:
    """Capital free to deploy: start + realized P&L - currently deployed."""
    return START_CAPITAL_USD + get_realized_pnl() - get_deployed_capital()


def open_position(sig):
    """Open a paper trade for a signal. Position size already in USD from signal."""
    entry_price = sig["entry_price"]
    position_usd = sig["position_size_usd"]
    if entry_price <= 0 or position_usd <= 0:
        return False, "invalid entry price or size"

    # CAPITAL CAP: refuse to open if not enough free capital
    avail = available_capital()
    if position_usd > avail:
        return False, f"insufficient capital (need ${position_usd:.2f}, have ${avail:.2f})"

    tokens = position_usd / entry_price
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO paper_trades
        (signal_id, symbol, address, strategy, opened_at,
         entry_price, position_size_usd, tokens_held,
         stop_loss, take_profit)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        sig["id"], sig["symbol"], sig["address"], sig["strategy"],
        int(time.time()), entry_price, position_usd, tokens,
        sig["stop_loss"], sig["take_profit"],
    ))
    conn.commit()
    conn.close()
    return True, f"opened at {entry_price:.8f} with {tokens:,.4f} tokens"


def close_position(trade_id, close_price, close_reason, tokens_held,
                    position_usd, entry_price):
    current_value = tokens_held * close_price
    pnl_usd = current_value - position_usd
    pnl_pct = (pnl_usd / position_usd) * 100 if position_usd else 0
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE paper_trades
        SET closed_at = ?, close_price = ?, close_reason = ?,
            pnl_usd = ?, pnl_pct = ?
        WHERE id = ?
    """, (int(time.time()), close_price, close_reason, pnl_usd, pnl_pct, trade_id))
    conn.commit()
    conn.close()
    return pnl_usd, pnl_pct


def check_position_exits(pos, current_price, current_ts):
    """Returns (should_close, reason) for an open position."""
    # HARD MAX-LOSS CIRCUIT BREAKER (added 2026-05-21)
    if current_price and current_price > 0 and pos.get('entry_price'):
        loss_pct = (current_price - pos['entry_price']) / pos['entry_price']
        if loss_pct <= -MAX_LOSS_PCT_HARD:
            return True, f'HARD_STOP at {loss_pct*100:.1f}% (max -{MAX_LOSS_PCT_HARD*100:.0f}%)'

    if current_price is None:
        return False, "no current price"

    # Hard stop loss
    if current_price <= pos["stop_loss"]:
        return True, f"STOP_LOSS hit at {current_price:.8f}"

    # Take profit
    if pos["take_profit"] and current_price >= pos["take_profit"]:
        return True, f"TAKE_PROFIT hit at {current_price:.8f}"

    # Time stop - max position age
    age_hours = (time.time() - pos["opened_at"]) / 3600
    if age_hours >= MAX_POSITION_AGE_HOURS:
        return True, f"TIME_STOP at {age_hours:.0f}h (max {MAX_POSITION_AGE_HOURS}h)"

    # Whale risk crossing 60 mid-trade
    wr = get_whale_risk_now(pos["address"])
    if wr is not None and wr >= 60:
        return True, f"WHALE_RISK escalated to {wr:.0f}"

    return False, ""


def run_paper_trader(console, dry_run):
    init_paper_db()
    console.print("[bold]Paper Trader[/bold]\n")

    # === Step 1: Open new positions from pending signals ===
    new_signals = get_unfilled_buy_signals()
    opened = []
    if new_signals:
        console.print(f"[cyan]Opening positions for {len(new_signals)} new signal(s)...[/cyan]")
        # Get addresses with existing open positions to prevent duplicates
        conn_check = sqlite3.connect(DB_PATH)
        open_addresses = {r[0] for r in conn_check.execute(
            "SELECT address FROM paper_trades WHERE closed_at IS NULL"
        ).fetchall()}
        conn_check.close()
        for sig in new_signals:
            if sig["address"] in open_addresses:
                console.print(f"  [yellow]SKIP[/yellow] {sig['symbol']}: already has open position")
                continue
            if dry_run:
                console.print(f"  [dim](dry-run) would open {sig['symbol']} at {sig['entry_price']:.8f}[/dim]")
                continue
            ok, msg = open_position(sig)
            mark = "[green]OK[/green]" if ok else "[red]FAIL[/red]"
            console.print(f"  {mark} {sig['symbol']}: {msg}")
            if ok:
                opened.append(sig["symbol"])
                open_addresses.add(sig["address"])
    else:
        console.print("[dim]No new BUY signals to open.[/dim]")

    # === Step 2: Check exits for open positions ===
    positions = get_open_positions()
    closed = []
    if positions:
        console.print(f"\n[cyan]Checking exits for {len(positions)} open position(s)...[/cyan]")
        for pos in positions:
            current_price, current_ts = get_current_price(pos["address"])
            should_close, reason = check_position_exits(pos, current_price, current_ts)
            if should_close:
                if dry_run:
                    console.print(f"  [dim](dry-run) would close {pos['symbol']}: {reason}[/dim]")
                    continue
                pnl_usd, pnl_pct = close_position(
                    pos["id"], current_price, reason,
                    pos["tokens_held"], pos["position_size_usd"], pos["entry_price"],
                )
                color = "green" if pnl_usd >= 0 else "red"
                console.print(f"  [{color}]CLOSED[/{color}] {pos['symbol']}: {reason} | P&L: ${pnl_usd:+.2f} ({pnl_pct:+.1f}%)")
                closed.append((pos["symbol"], pnl_usd))

    # === Step 3: Show current portfolio state ===
    show_portfolio_summary(console)


def show_portfolio_summary(console):
    conn = sqlite3.connect(DB_PATH)

    # Open positions with current marks
    open_rows = conn.execute("""
        SELECT id, symbol, address, strategy, opened_at,
               entry_price, position_size_usd, tokens_held,
               stop_loss, take_profit
        FROM paper_trades
        WHERE closed_at IS NULL
    """).fetchall()

    # Closed positions
    closed_rows = conn.execute("""
        SELECT symbol, strategy, opened_at, closed_at,
               entry_price, close_price, position_size_usd,
               pnl_usd, pnl_pct, close_reason
        FROM paper_trades
        WHERE closed_at IS NOT NULL
        ORDER BY closed_at DESC
        LIMIT 20
    """).fetchall()

    # === Open positions table ===
    if open_rows:
        open_table = Table(title=f"Open Positions ({len(open_rows)})")
        open_table.add_column("Symbol")
        open_table.add_column("Strategy")
        open_table.add_column("Age", justify="right")
        open_table.add_column("Entry", justify="right")
        open_table.add_column("Current", justify="right")
        open_table.add_column("Size USD", justify="right")
        open_table.add_column("Unrealized", justify="right")
        open_table.add_column("Stop", justify="right")

        total_unrealized = 0
        for row in open_rows:
            id_, sym, addr, strat, opened_at, entry, size, tokens, stop, tp = row
            current_price, _ = get_current_price(addr)
            if current_price:
                current_value = tokens * current_price
                unrealized = current_value - size
                unrealized_pct = (unrealized / size) * 100
                color = "green" if unrealized >= 0 else "red"
                unrealized_str = f"[{color}]${unrealized:+.2f} ({unrealized_pct:+.1f}%)[/{color}]"
                current_str = f"{current_price:.8f}"
                total_unrealized += unrealized
            else:
                unrealized_str = "[dim]no price[/dim]"
                current_str = "-"

            age_h = (time.time() - opened_at) / 3600
            open_table.add_row(
                sym[:10], strat,
                f"{age_h:.1f}h",
                f"{entry:.8f}",
                current_str,
                f"${size:.2f}",
                unrealized_str,
                f"{stop:.8f}",
            )
        console.print(open_table)
        color = "green" if total_unrealized >= 0 else "red"
        console.print(f"\n[{color}]Total unrealized: ${total_unrealized:+.2f}[/{color}]")

    # === Recent closed trades ===
    if closed_rows:
        closed_table = Table(title=f"Recent Closed Trades")
        closed_table.add_column("Symbol")
        closed_table.add_column("Strategy")
        closed_table.add_column("Duration", justify="right")
        closed_table.add_column("Entry", justify="right")
        closed_table.add_column("Exit", justify="right")
        closed_table.add_column("Size", justify="right")
        closed_table.add_column("P&L", justify="right")
        closed_table.add_column("Reason")

        for row in closed_rows:
            sym, strat, op, cl, entry, exit_p, size, pnl, pnl_pct, reason = row
            duration_h = (cl - op) / 3600
            color = "green" if pnl >= 0 else "red"
            closed_table.add_row(
                sym[:10], strat,
                f"{duration_h:.1f}h",
                f"{entry:.8f}",
                f"{exit_p:.8f}" if exit_p else "-",
                f"${size:.2f}",
                f"[{color}]${pnl:+.2f} ({pnl_pct:+.1f}%)[/{color}]",
                (reason or "")[:30],
            )
        console.print(closed_table)

    # === Aggregate stats ===
    stats = conn.execute("""
        SELECT
            COUNT(*) as total_closed,
            SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) as losses,
            COALESCE(SUM(pnl_usd), 0) as total_pnl,
            COALESCE(AVG(pnl_usd), 0) as avg_pnl,
            COALESCE(AVG(CASE WHEN pnl_usd > 0 THEN pnl_usd END), 0) as avg_win,
            COALESCE(AVG(CASE WHEN pnl_usd < 0 THEN pnl_usd END), 0) as avg_loss
        FROM paper_trades
        WHERE closed_at IS NOT NULL
    """).fetchone()

    total, wins, losses, total_pnl, avg_pnl, avg_win, avg_loss = stats
    conn.close()

    if total > 0:
        win_rate = (wins / total * 100) if total else 0
        expectancy = avg_pnl
        console.print(f"\n[bold]Closed Trade Stats[/bold]")
        console.print(f"  Total closed: {total}")
        console.print(f"  Wins: {wins} ({win_rate:.0f}%)  Losses: {losses}")
        pnl_color = "green" if total_pnl >= 0 else "red"
        console.print(f"  Total realized P&L: [{pnl_color}]${total_pnl:+.2f}[/{pnl_color}]")
        console.print(f"  Avg win: ${avg_win:+.2f}  Avg loss: ${avg_loss:+.2f}")
        console.print(f"  Expectancy per trade: ${expectancy:+.2f}")

    if not open_rows and not closed_rows:
        console.print("\n[dim]No paper trades yet. Run strategy.py to generate BUY signals,[/dim]")
        console.print("[dim]then re-run paper_trader.py to open simulated positions.[/dim]")


@click.command()
@click.option("--dry-run", is_flag=True, help="Show what would happen without writing to DB.")
def main(dry_run):
    console = Console()
    run_paper_trader(console, dry_run)


if __name__ == "__main__":
    main()