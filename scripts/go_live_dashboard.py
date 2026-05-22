"""Should-I-go-live dashboard.

Computes running statistics over paper trades and reports whether the strategy
has produced enough evidence of edge to go live with real money.

The dashboard does not make the decision for you, but it makes the answer
obvious in either direction by comparing your actual results against
clear thresholds.

Default thresholds (conservative):
  Minimum trades:          50
  Minimum win rate:        55%
  Minimum expectancy:      $0.10 per trade
  Minimum days of data:    14
  Maximum drawdown:        25%
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = Path('data/memecoins.db')


# Default go-live thresholds
DEFAULTS = {
    'min_trades': 50,
    'min_win_rate_pct': 55.0,
    'min_expectancy_usd': 0.10,
    'min_days_of_data': 14,
    'max_drawdown_pct': 25.0,
}


def get_paper_trades():
    """All closed paper trades excluding DEDUPE_CLEANUP."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT pnl_usd, pnl_pct, strategy, close_reason, opened_at, closed_at
        FROM paper_trades
        WHERE closed_at IS NOT NULL
          AND close_reason != 'DEDUPE_CLEANUP'
        ORDER BY closed_at ASC
    """).fetchall()
    conn.close()
    return rows


def compute_running_drawdown(pnls: list[float], start_capital: float):
    """Walk forward through pnls, track equity peak and current drawdown."""
    equity = start_capital
    peak = start_capital
    max_dd_pct = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd_pct = (peak - equity) / peak * 100 if peak > 0 else 0
        max_dd_pct = max(max_dd_pct, dd_pct)
    return equity, peak, max_dd_pct


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start-capital', type=float, default=100.0)
    parser.add_argument('--min-trades', type=int, default=DEFAULTS['min_trades'])
    parser.add_argument('--min-win-rate', type=float, default=DEFAULTS['min_win_rate_pct'])
    parser.add_argument('--min-expectancy', type=float, default=DEFAULTS['min_expectancy_usd'])
    parser.add_argument('--min-days', type=int, default=DEFAULTS['min_days_of_data'])
    parser.add_argument('--max-drawdown', type=float, default=DEFAULTS['max_drawdown_pct'])
    args = parser.parse_args()

    console = Console()
    console.print()
    console.print('[bold]Should I Go Live? Dashboard[/bold]')
    console.print(f'Generated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}')
    console.print()

    trades = get_paper_trades()
    if not trades:
        console.print('[yellow]No closed paper trades yet. Cannot evaluate. Run the bot longer.[/yellow]')
        return

    pnls = [r[0] or 0 for r in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    expectancy = total_pnl / len(trades) if trades else 0
    earliest_ts = min(r[4] for r in trades) if trades else int(time.time())
    days_of_data = (time.time() - earliest_ts) / 86400

    end_equity, peak_equity, max_dd_pct = compute_running_drawdown(pnls, args.start_capital)
    return_pct = (end_equity / args.start_capital - 1) * 100

    # Per-strategy
    by_strat = defaultdict(list)
    for pnl, pct, strat, reason, op, cl in trades:
        by_strat[strat].append(pnl or 0)

    # Stats table
    t = Table(title='Strategy Performance (paper)')
    t.add_column('Metric')
    t.add_column('Value', justify='right')
    t.add_row('Closed trades', f'{len(trades)}')
    t.add_row('Days of data', f'{days_of_data:.1f}')
    t.add_row('Wins / Losses', f'{len(wins)} / {len(losses)}')
    t.add_row('Win rate', f'{win_rate:.1f}%')
    t.add_row('Avg win', f'+${avg_win:.2f}')
    t.add_row('Avg loss', f'${avg_loss:.2f}')
    t.add_row('Total P&L', f'${total_pnl:+.2f}')
    t.add_row('Expectancy per trade', f'${expectancy:+.2f}')
    t.add_row('Equity ($100 start)', f'${end_equity:.2f} ({return_pct:+.1f}%)')
    t.add_row('Max drawdown', f'{max_dd_pct:.1f}%')
    console.print(t)

    # Per-strategy
    if by_strat:
        s = Table(title='Per-Strategy Breakdown')
        s.add_column('Strategy')
        s.add_column('Trades', justify='right')
        s.add_column('Win Rate', justify='right')
        s.add_column('Avg P&L', justify='right')
        s.add_column('Total P&L', justify='right')
        for strat, plist in by_strat.items():
            sw = sum(1 for p in plist if p > 0)
            wr = sw / len(plist) * 100 if plist else 0
            avg = sum(plist) / len(plist) if plist else 0
            tot = sum(plist)
            s.add_row(strat, str(len(plist)),
                      f'{wr:.0f}%', f'${avg:+.2f}', f'${tot:+.2f}')
        console.print(s)

    # === Threshold checks ===
    console.print()
    console.print('[bold]Readiness Checks[/bold]')

    checks = []
    checks.append((
        f'Minimum trades (>={args.min_trades})',
        len(trades) >= args.min_trades,
        f'{len(trades)} / {args.min_trades}',
    ))
    checks.append((
        f'Minimum win rate (>={args.min_win_rate:.0f}%)',
        win_rate >= args.min_win_rate,
        f'{win_rate:.1f}% / {args.min_win_rate:.0f}%',
    ))
    checks.append((
        f'Minimum expectancy (>=${args.min_expectancy:.2f})',
        expectancy >= args.min_expectancy,
        f'${expectancy:+.2f} / ${args.min_expectancy:+.2f}',
    ))
    checks.append((
        f'Minimum days of data (>={args.min_days})',
        days_of_data >= args.min_days,
        f'{days_of_data:.1f}d / {args.min_days}d',
    ))
    checks.append((
        f'Max drawdown (<={args.max_drawdown:.0f}%)',
        max_dd_pct <= args.max_drawdown,
        f'{max_dd_pct:.1f}% / {args.max_drawdown:.0f}%',
    ))

    check_table = Table()
    check_table.add_column('Check')
    check_table.add_column('Status')
    check_table.add_column('Detail', justify='right')
    for label, passed, detail in checks:
        status = '[green]PASS[/green]' if passed else '[red]FAIL[/red]'
        check_table.add_row(label, status, detail)
    console.print(check_table)

    n_passed = sum(1 for _, p, _ in checks if p)
    n_total = len(checks)
    console.print()

    if n_passed == n_total:
        console.print('[bold green]ALL CHECKS PASS - strategy is ready for live trading.[/bold green]')
        console.print('Recommended next step: enable execution coordinator in monitor loop,')
        console.print('start with $30/day cap, manual confirmation per trade.')
    else:
        console.print(f'[bold yellow]NOT YET READY: {n_passed}/{n_total} checks passed.[/bold yellow]')
        console.print('Continue paper trading until all checks pass.')
        console.print('Re-run this dashboard daily to track progress.')


if __name__ == '__main__':
    main()
