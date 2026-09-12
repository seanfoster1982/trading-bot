"""Telegram portfolio digest - read-only status push.

Builds a human-readable snapshot of the paper-trading account and (optionally)
sends it to Telegram. This is purely additive: it only READS the database and
the monitor log. It does not touch strategy logic, the pipeline, or any
trade-writing code, so it is safe to run while the live data-collection run is
in progress.

Usage:
    python scripts/bot_report.py            # print to console only
    python scripts/bot_report.py --send     # also push to Telegram

Designed to be cwd-independent (resolves all paths from this file's location)
so it works correctly when launched by a Windows scheduled task.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "memecoins.db"
LOG_PATH = ROOT / "data" / "overnight.log"

# Reuse the (now cwd-safe) Telegram notifier.
sys.path.insert(0, str(ROOT / "scripts"))
import market_sniper  # noqa: E402
import telegram_notifier  # noqa: E402
import whale_trace  # noqa: E402


START_CAPITAL_USD = 100.0


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def get_current_price(conn: sqlite3.Connection, address: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM indicators
        WHERE address = ? AND interval = '5m'
        ORDER BY timestamp DESC LIMIT 1
        """,
        (address,),
    ).fetchone()
    return row["close"] if row else None


def gather_stats(conn: sqlite3.Connection) -> dict:
    closed = conn.execute(
        """
        SELECT COUNT(*) n,
               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins,
               SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) losses,
               COALESCE(SUM(pnl_usd), 0) total_pnl
        FROM paper_trades WHERE closed_at IS NOT NULL
        """
    ).fetchone()

    per_strategy = conn.execute(
        """
        SELECT strategy,
               COUNT(*) n,
               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins,
               COALESCE(SUM(pnl_usd), 0) total
        FROM paper_trades WHERE closed_at IS NOT NULL
        GROUP BY strategy
        ORDER BY total DESC
        """
    ).fetchall()

    open_rows = conn.execute(
        """
        SELECT symbol, address, strategy, opened_at,
               entry_price, position_size_usd, tokens_held
        FROM paper_trades WHERE closed_at IS NULL
        ORDER BY opened_at ASC
        """
    ).fetchall()

    return {"closed": closed, "per_strategy": per_strategy, "open_rows": open_rows}


def last_cycle_age_min() -> tuple[str, float] | None:
    """Return (timestamp_str, age_minutes) of the most recent bot activity.

    Uses the market_pulse table (written every sniper cycle) and whale wallet
    checkpoints — the retired pipeline's overnight.log is no longer updated.
    """
    conn = _conn()
    try:
        stamps = []
        for query in (
            "SELECT MAX(timestamp) FROM market_pulse",
            "SELECT MAX(last_checked_at) FROM whale_wallets",
        ):
            try:
                row = conn.execute(query).fetchone()
                if row and row[0]:
                    stamps.append(row[0])
            except sqlite3.OperationalError:
                continue
    finally:
        conn.close()
    if not stamps:
        return None
    latest = max(stamps)
    stamp = datetime.fromtimestamp(latest, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    age_min = (time.time() - latest) / 60
    return stamp, age_min


def build_report() -> str:
    conn = _conn()
    try:
        stats = gather_stats(conn)
        closed = stats["closed"]
        n = closed["n"] or 0
        wins = closed["wins"] or 0
        losses = closed["losses"] or 0
        total_pnl = closed["total_pnl"] or 0.0
        win_rate = (100 * wins / n) if n else 0.0
        equity = START_CAPITAL_USD + total_pnl

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            "<b>Trading Bot - Portfolio Digest</b>",
            f"<i>{now}</i>",
            "",
            f"Equity: <b>${equity:,.2f}</b> ({total_pnl:+.2f} vs $100 start)",
            f"Closed: {n}  |  Win rate: {win_rate:.0f}% ({wins}W/{losses}L)",
        ]

        lines.append("")
        lines.append("<b>By strategy</b>")
        if stats["per_strategy"]:
            for r in stats["per_strategy"]:
                s_n = r["n"] or 0
                s_w = r["wins"] or 0
                s_wr = (100 * s_w / s_n) if s_n else 0
                lines.append(
                    f"  {r['strategy']}: {s_n} trades, {s_wr:.0f}% WR, "
                    f"{r['total']:+.2f}"
                )
        else:
            lines.append("  (no closed trades yet)")

        lines.append("")
        open_rows = stats["open_rows"]
        if open_rows:
            lines.append(f"<b>Open positions ({len(open_rows)})</b>")
            total_unreal = 0.0
            for r in open_rows:
                cur = get_current_price(conn, r["address"])
                age_h = (time.time() - r["opened_at"]) / 3600
                if cur:
                    unreal = r["tokens_held"] * cur - r["position_size_usd"]
                    unreal_pct = (unreal / r["position_size_usd"] * 100) if r["position_size_usd"] else 0
                    total_unreal += unreal
                    lines.append(
                        f"  {r['symbol']} ({r['strategy']}): "
                        f"{unreal:+.2f} ({unreal_pct:+.1f}%), age {age_h:.0f}h"
                    )
                else:
                    lines.append(
                        f"  {r['symbol']} ({r['strategy']}): no price, age {age_h:.0f}h"
                    )
            lines.append(f"  <b>Unrealized: {total_unreal:+.2f}</b>")
        else:
            lines.append("<b>Open positions:</b> none")

        # Whale Trace — separate shadow-copy analysis, appended read-only.
        try:
            lines.append("")
            lines.append(whale_trace.build_report())
        except Exception as e:
            lines.append(f"(whale trace report unavailable: {type(e).__name__})")

        # Market Sniper — separate shadow strategies + market pulse.
        try:
            lines.append("")
            lines.append(market_sniper.build_report())
        except Exception as e:
            lines.append(f"(sniper report unavailable: {type(e).__name__})")

        try:
            import rh_live_trader
            lines.append("")
            lines.append(rh_live_trader.build_report())
        except Exception as e:
            lines.append(f"(robinhood live report unavailable: {type(e).__name__})")

        # Loop health
        lines.append("")
        cyc = last_cycle_age_min()
        if cyc:
            stamp, age_min = cyc
            health = "OK" if age_min < 30 else "STALE?"
            lines.append(f"Last cycle: {stamp} ({age_min:.0f} min ago) [{health}]")
        else:
            lines.append("Last cycle: unknown (no log yet)")

        return "\n".join(lines)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a paper-trading portfolio digest.")
    parser.add_argument("--send", action="store_true", help="Push the report to Telegram.")
    args = parser.parse_args()

    report = build_report()
    # Console output uses plain text; strip the simple HTML tags for readability.
    console_text = (
        report.replace("<b>", "").replace("</b>", "")
        .replace("<i>", "").replace("</i>", "")
    )
    print(console_text)

    if args.send:
        ok = telegram_notifier.send(report)
        print(f"\n[telegram] sent: {ok}")
        if not ok:
            print("[telegram] not configured or send failed.", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
