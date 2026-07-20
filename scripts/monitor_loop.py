"""Overnight monitoring loop.

Runs the full pipeline every INTERVAL_MINUTES. Logs to:
  data/overnight.log  - one line per pipeline run, summary
  data/alerts.log     - separate log for BUY signals, position closes, errors

Stop with Ctrl+C. Safe to leave running.

Usage:
  python scripts/monitor_loop.py
  python scripts/monitor_loop.py --interval 15   (override default 30 min)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

# Telegram notifier - no-ops if env vars not set
sys_path_inserted = True
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from scripts.telegram_notifier import send as tg_send, is_configured as tg_configured
from pathlib import Path

# Whale product config (screener args, interval docs)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from whale_config import WHALE_ONLY, SCREENER_ARGS, PIPELINE_INTERVAL_MINUTES  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / 'data' / 'memecoins.db'
LOG_PATH = ROOT / 'data' / 'overnight.log'
ALERT_PATH = ROOT / 'data' / 'alerts.log'

# Pipeline order — same as the manual sequence.
# Each entry is (script_name, extra_cli_args).
PIPELINE = [
    ('screen_memecoins.py', SCREENER_ARGS if WHALE_ONLY else []),
    ('watchlist.py', []),
    ('rug_check.py', []),
    ('ingest_ohlcv.py', []),
    ('compute_indicators.py', []),
    ('whale_risk.py', []),
    ('macro_regime.py', []),
    ('strategy.py', []),
    ('paper_trader.py', []),
    # Separate analysis — shadow-follows top-PnL wallets. Writes only to its
    # own tables, never to signals/paper_trades.
    ('whale_trace.py', []),
]


def ts() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def append_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def run_script(script_name: str, extra_args: list[str] | None = None, timeout_sec: int = 600) -> tuple[bool, str]:
    """Run a pipeline script. Returns (success, last_line_of_output)."""
    script_path = ROOT / 'scripts' / script_name
    cmd = [sys.executable, str(script_path)] + (extra_args or [])
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            encoding='utf-8',
            errors='replace',
            env={**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8'},
        )
        ok = result.returncode == 0
        # Grab last non-empty line of stdout for the summary
        out_lines = [l for l in result.stdout.splitlines() if l.strip()]
        last_line = out_lines[-1] if out_lines else ''
        if not ok:
            err_lines = [l for l in result.stderr.splitlines() if l.strip()]
            last_line = (err_lines[-1] if err_lines else 'unknown error')[:200]
        return ok, last_line
    except subprocess.TimeoutExpired:
        return False, f'timeout after {timeout_sec}s'
    except Exception as e:
        return False, f'exception: {str(e)[:200]}'


def query_recent_signals(since_ts: int) -> list[dict]:
    """Only return signals that resulted in a NEW paper trade opening after since_ts.
    Filters out signals that were duplicates or rejected by dedupe."""
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """SELECT pt.symbol, pt.strategy, pt.entry_price, pt.stop_loss,
                      pt.position_size_usd, pt.opened_at
               FROM paper_trades pt
               WHERE pt.opened_at >= ?
                 AND pt.close_reason IS NOT 'DEDUPE_CLEANUP'""",
            (since_ts,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [
        {'symbol': r[0], 'strategy': r[1], 'entry': r[2],
         'stop': r[3], 'size': r[4], 'ts': r[5]} for r in rows
    ]


def query_recent_paper_closes(since_ts: int) -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """SELECT symbol, strategy, pnl_usd, pnl_pct, close_reason, closed_at
               FROM paper_trades
               WHERE closed_at IS NOT NULL AND closed_at >= ?""",
            (since_ts,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [
        {'symbol': r[0], 'strategy': r[1], 'pnl_usd': r[2],
         'pnl_pct': r[3], 'reason': r[4], 'ts': r[5]} for r in rows
    ]


def query_open_position_count() -> int:
    if not DB_PATH.exists():
        return 0
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            'SELECT COUNT(*) FROM paper_trades WHERE closed_at IS NULL'
        ).fetchone()
    except sqlite3.OperationalError:
        conn.close()
        return 0
    conn.close()
    return row[0] if row else 0


def run_pipeline_once(prev_end_ts: int) -> dict:
    """Run all pipeline scripts. Return summary dict."""
    start_ts = int(time.time())
    results = {}
    errors = []

    for script, extra_args in PIPELINE:
        ok, msg = run_script(script, extra_args)
        results[script] = {'ok': ok, 'msg': msg}
        if not ok:
            errors.append(f'{script}: {msg}')

    # Check for new signals and closed positions since last run
    new_signals = query_recent_signals(prev_end_ts)
    new_closes = query_recent_paper_closes(prev_end_ts)
    open_count = query_open_position_count()

    return {
        'start_ts': start_ts,
        'end_ts': int(time.time()),
        'duration_sec': int(time.time()) - start_ts,
        'errors': errors,
        'new_signals': new_signals,
        'new_closes': new_closes,
        'open_positions': open_count,
    }


def write_summary_line(summary: dict) -> None:
    line = (
        f"{ts()} | duration={summary['duration_sec']}s | "
        f"open_positions={summary['open_positions']} | "
        f"new_buys={len(summary['new_signals'])} | "
        f"new_closes={len(summary['new_closes'])} | "
        f"errors={len(summary['errors'])}"
    )
    append_log(LOG_PATH, line)


def write_alerts(summary: dict) -> None:
    """Write notable events to the alerts log AND send to Telegram."""
    tg_on = tg_configured()
    for sig in summary['new_signals']:
        if tg_on:
            tg_send(
                f"<b>BUY SIGNAL</b>\n"
                f"<b>{sig['symbol']}</b> ({sig['strategy']})\n"
                f"Entry: {sig['entry']:.8f}\n"
                f"Stop: {sig['stop']:.8f}\n"
                f"Size: ${sig['size']:.2f}"
            )
        append_log(ALERT_PATH, (
            f"{ts()} | BUY_SIGNAL | {sig['symbol']} | {sig['strategy']} | "
            f"entry={sig['entry']:.8f} stop={sig['stop']:.8f} size=${sig['size']:.2f}"
        ))
    for cl in summary['new_closes']:
        sign = '+' if cl['pnl_usd'] >= 0 else ''
        if tg_on:
            emoji = "✓" if cl['pnl_usd'] >= 0 else "✗"
            tg_send(
                f"<b>{emoji} POSITION CLOSED</b>\n"
                f"<b>{cl['symbol']}</b> ({cl['strategy']})\n"
                f"P&L: {sign}${cl['pnl_usd']:.2f} ({sign}{cl['pnl_pct']:.1f}%)\n"
                f"Reason: {cl['reason']}"
            )
        append_log(ALERT_PATH, (
            f"{ts()} | POSITION_CLOSED | {cl['symbol']} | {cl['strategy']} | "
            f"pnl={sign}${cl['pnl_usd']:.2f} ({sign}{cl['pnl_pct']:.1f}%) | {cl['reason']}"
        ))
    for err in summary['errors']:
        if tg_on:
            tg_send(f"<b>BOT ERROR</b>\n<code>{err[:300]}</code>", silent=True)
        append_log(ALERT_PATH, f"{ts()} | ERROR | {err}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--interval', type=int, default=30,
                        help='Minutes between pipeline runs (default 30)')
    parser.add_argument('--once', action='store_true',
                        help='Run pipeline once and exit')
    args = parser.parse_args()

    interval_sec = args.interval * 60

    print(f'[{ts()}] Monitor loop starting. Interval: {args.interval} min.')
    if WHALE_ONLY:
        print(f'[{ts()}] Mode: WHALE COPY ONLY (recommended interval: {PIPELINE_INTERVAL_MINUTES} min)')
    print(f'[{ts()}] Logs: {LOG_PATH} and {ALERT_PATH}')
    print(f'[{ts()}] Press Ctrl+C to stop.')
    print()

    append_log(LOG_PATH, f'{ts()} | MONITOR_STARTED | interval={args.interval}min | whale_only={WHALE_ONLY}')
    prev_end_ts = int(time.time()) - 60  # look back 1 min on first run

    try:
        while True:
            print(f'[{ts()}] Running pipeline...')
            summary = run_pipeline_once(prev_end_ts)
            write_summary_line(summary)
            write_alerts(summary)
            prev_end_ts = summary['end_ts']

            print(f"[{ts()}] Done in {summary['duration_sec']}s. "
                  f"Open positions: {summary['open_positions']}. "
                  f"New signals: {len(summary['new_signals'])}. "
                  f"Errors: {len(summary['errors'])}.")

            if summary['new_signals']:
                for sig in summary['new_signals']:
                    print(f"  [ALERT] BUY {sig['symbol']} @ {sig['entry']:.8f}")
            if summary['errors']:
                for err in summary['errors']:
                    print(f"  [ERROR] {err}")

            if args.once:
                break

            print(f'[{ts()}] Sleeping {args.interval} min until next run...')
            print()
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        print()
        print(f'[{ts()}] Stopped by user.')
        append_log(LOG_PATH, f'{ts()} | MONITOR_STOPPED | by user')


if __name__ == '__main__':
    main()
