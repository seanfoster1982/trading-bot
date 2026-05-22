"""Daily report - summarize bot activity over a given window.

Reads from signals, paper_trades, and live_trades tables and produces a clean
text summary you can read in the morning. Default window: last 24 hours.

Usage:
  python scripts/daily_report.py
  python scripts/daily_report.py --hours 48
  python scripts/daily_report.py --since 2026-05-12
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = Path('data/memecoins.db')


def fmt_pnl(usd: float) -> str:
    sign = '+' if usd >= 0 else ''
    return f'{sign}${usd:.2f}'


def fmt_pct(pct: float) -> str:
    sign = '+' if pct >= 0 else ''
    return f'{sign}{pct:.1f}%'


def get_signals_in_window(since_ts: int):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT symbol, strategy, action, entry_price, position_size_usd, generated_at
        FROM signals
        WHERE generated_at >= ?
        ORDER BY generated_at ASC
    """, (since_ts,)).fetchall()
    conn.close()
    return rows


def get_paper_trades_in_window(since_ts: int):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT symbol, strategy, opened_at, closed_at, entry_price,
               close_price, position_size_usd, pnl_usd, pnl_pct, close_reason
        FROM paper_trades
        WHERE (closed_at IS NULL AND opened_at >= ?)
           OR (closed_at IS NOT NULL AND closed_at >= ?)
        ORDER BY COALESCE(closed_at, opened_at) DESC
    """, (since_ts, since_ts)).fetchall()
    conn.close()
    return rows


def get_live_trades_in_window(since_ts: int):
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("""
            SELECT symbol, strategy, opened_at, closed_at,
                   entry_usdc_spent, close_usdc_received,
                   pnl_usd, pnl_pct, close_reason, entry_signature, close_signature
            FROM live_trades
            WHERE (closed_at IS NULL AND opened_at >= ?)
               OR (closed_at IS NOT NULL AND closed_at >= ?)
            ORDER BY COALESCE(closed_at, opened_at) DESC
        """, (since_ts, since_ts)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


def get_all_paper_closed():
    """All-time closed paper trades for running stats."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT pnl_usd, pnl_pct, strategy, close_reason
        FROM paper_trades
        WHERE closed_at IS NOT NULL
    """).fetchall()
    conn.close()
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hours', type=int, default=24)
    parser.add_argument('--since', type=str, default=None, help='YYYY-MM-DD')
    args = parser.parse_args()

    if args.since:
        since_dt = datetime.strptime(args.since, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        since_ts = int(since_dt.timestamp())
        window_desc = f'since {args.since}'
    else:
        since_ts = int(time.time()) - args.hours * 3600
        window_desc = f'last {args.hours}h'

    console = Console()
    console.print()
    console.print(f'[bold]Daily Report ({window_desc})[/bold]')
    console.print(f'Generated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}')
    console.print()

    # === Signals ===
    signals = get_signals_in_window(since_ts)
    console.print(f'[bold]Signals fired:[/bold] {len(signals)}')

    if signals:
        sig_by_symbol = defaultdict(int)
        for sym, strat, action, ep, size, ts in signals:
            sig_by_symbol[sym] += 1
        for sym, count in sorted(sig_by_symbol.items(), key=lambda x: -x[1]):
            console.print(f'  {sym}: {count} signal(s)')

    # === Paper trades ===
    paper = get_paper_trades_in_window(since_ts)
    open_paper = [r for r in paper if r[3] is None]  # closed_at is None
    closed_paper = [r for r in paper if r[3] is not None]

    console.print()
    console.print(f'[bold]Paper trades:[/bold] {len(open_paper)} open, {len(closed_paper)} closed in window')

    if closed_paper:
        wins = [r for r in closed_paper if r[7] and r[7] > 0]
        losses = [r for r in closed_paper if r[7] and r[7] <= 0]
        total_pnl = sum(r[7] or 0 for r in closed_paper)
        win_rate = len(wins) / len(closed_paper) * 100 if closed_paper else 0

        t = Table(title='Closed Paper Trades (in window)')
        t.add_column('Symbol')
        t.add_column('Strategy')
        t.add_column('Entry', justify='right')
        t.add_column('Exit', justify='right')
        t.add_column('P&L', justify='right')
        t.add_column('Reason')

        for sym, strat, op_ts, cl_ts, entry, exit_p, size, pnl, pnl_pct, reason in closed_paper:
            color = 'green' if (pnl or 0) >= 0 else 'red'
            t.add_row(
                sym[:12], strat,
                f'{entry:.8f}' if entry else '-',
                f'{exit_p:.8f}' if exit_p else '-',
                f'[{color}]{fmt_pnl(pnl or 0)} ({fmt_pct(pnl_pct or 0)})[/{color}]',
                (reason or '')[:25],
            )
        console.print(t)

        console.print(f'  Window P&L: [{("green" if total_pnl >= 0 else "red")}]{fmt_pnl(total_pnl)}[/]')
        console.print(f'  Window win rate: {win_rate:.0f}% ({len(wins)}W / {len(losses)}L)')

    if open_paper:
        t = Table(title='Open Paper Positions')
        t.add_column('Symbol')
        t.add_column('Strategy')
        t.add_column('Entry', justify='right')
        t.add_column('Age', justify='right')
        for sym, strat, op_ts, cl_ts, entry, exit_p, size, pnl, pnl_pct, reason in open_paper:
            age_h = (time.time() - op_ts) / 3600 if op_ts else 0
            t.add_row(sym[:12], strat, f'{entry:.8f}' if entry else '-', f'{age_h:.1f}h')
        console.print(t)

    # === Live trades ===
    live = get_live_trades_in_window(since_ts)
    if live:
        console.print()
        console.print(f'[bold]Live trades:[/bold] {len(live)} in window')
        t = Table(title='Live Trades (in window)')
        t.add_column('Symbol')
        t.add_column('Strategy')
        t.add_column('USDC In', justify='right')
        t.add_column('USDC Out', justify='right')
        t.add_column('P&L', justify='right')
        t.add_column('Status')
        for row in live:
            sym, strat, op, cl, usdc_in, usdc_out, pnl, pnl_pct, reason, esig, csig = row
            status = 'OPEN' if cl is None else f'CLOSED: {reason or ""}'[:30]
            color = 'green' if (pnl or 0) >= 0 else 'red'
            t.add_row(
                sym[:12], strat,
                f'${usdc_in:.2f}' if usdc_in else '-',
                f'${usdc_out:.2f}' if usdc_out else '-',
                f'[{color}]{fmt_pnl(pnl or 0)}[/{color}]' if pnl else '-',
                status,
            )
        console.print(t)

    # === All-time paper stats ===
    all_closed = get_all_paper_closed()
    if all_closed:
        wins = [r for r in all_closed if r[0] and r[0] > 0]
        losses = [r for r in all_closed if r[0] and r[0] <= 0]
        total_pnl = sum(r[0] or 0 for r in all_closed)
        avg_win = sum(r[0] for r in wins) / len(wins) if wins else 0
        avg_loss = sum(r[0] for r in losses) / len(losses) if losses else 0
        win_rate = len(wins) / len(all_closed) * 100 if all_closed else 0
        expectancy = total_pnl / len(all_closed) if all_closed else 0

        console.print()
        console.print(f'[bold]All-time paper stats:[/bold]')
        console.print(f'  Trades closed: {len(all_closed)}')
        console.print(f'  Win rate: {win_rate:.1f}% ({len(wins)}W / {len(losses)}L)')
        console.print(f'  Avg win: {fmt_pnl(avg_win)}  Avg loss: {fmt_pnl(avg_loss)}')
        color = 'green' if total_pnl >= 0 else 'red'
        console.print(f'  Total P&L: [{color}]{fmt_pnl(total_pnl)}[/{color}]')
        console.print(f'  Expectancy per trade: {fmt_pnl(expectancy)}')

        # Per-strategy breakdown
        by_strat = defaultdict(list)
        for pnl, pct, strat, reason in all_closed:
            by_strat[strat].append(pnl or 0)
        console.print()
        console.print(f'  [bold]Per-strategy:[/bold]')
        for strat, pnls in by_strat.items():
            sw = sum(1 for p in pnls if p > 0)
            wr = sw / len(pnls) * 100 if pnls else 0
            total = sum(pnls)
            console.print(f'    {strat}: {len(pnls)} trades, {wr:.0f}% win rate, total {fmt_pnl(total)}')


if __name__ == '__main__':
    main()
