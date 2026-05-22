"""Deep backtest of copy-trading the 4 directional whales we identified.

Pulls extended trade history per wallet, fixes the market resolution lookup
to use token IDs (which is what the trades data actually contains), and
runs three lag scenarios in one shot.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
import httpx
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


def parse_ts(value) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        s = str(value)
        if s.replace(".", "").isdigit():
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def to_float(v, default=0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


@dataclass
class Trade:
    timestamp: datetime
    market_title: str
    asset_id: str   # this is the TOKEN ID — what we use for resolution lookup
    condition_id: str
    side: str
    outcome: str
    price: float
    size: float


@dataclass
class CopyTrade:
    wallet: str
    market: str
    asset_id: str
    whale_entry_ts: datetime
    whale_price: float
    our_price: float
    size_usd: float
    final_price: float
    pnl_usd: float


async def fetch_user_trades_paginated(
    client: httpx.AsyncClient,
    wallet: str,
    *,
    pages: int = 5,
    page_size: int = 500,
) -> list[dict]:
    """Walk multiple pages of a wallet's trade history. Polymarket Data API
    pagination uses offset parameter."""
    all_trades: list[dict] = []
    for page in range(pages):
        try:
            r = await client.get(
                f"{DATA_API}/trades",
                params={
                    "user": wallet,
                    "limit": page_size,
                    "offset": page * page_size,
                },
                timeout=20.0,
            )
            if r.status_code != 200:
                break
            data = r.json()
            page_trades = data if isinstance(data, list) else data.get("trades", []) or []
            if not page_trades:
                break
            all_trades.extend(page_trades)
            if len(page_trades) < page_size:
                break  # last page
        except Exception:
            break
    return all_trades


def parse_trade(raw: dict) -> Trade | None:
    ts = parse_ts(raw.get("timestamp") or raw.get("matchtime") or raw.get("created_at"))
    if not ts:
        return None
    side = (raw.get("side") or raw.get("action") or "").upper()
    if side not in ("BUY", "SELL"):
        return None
    price = to_float(raw.get("price"))
    size = to_float(raw.get("size") or raw.get("amount"))
    if price <= 0 or size <= 0:
        return None
    asset_id = str(raw.get("asset") or raw.get("asset_id") or raw.get("tokenId") or "")
    if not asset_id:
        return None
    condition_id = str(raw.get("conditionId") or raw.get("condition_id") or "")
    title = raw.get("title") or raw.get("question") or raw.get("slug") or "?"
    outcome = raw.get("outcome") or ""
    return Trade(
        timestamp=ts,
        market_title=str(title)[:80],
        asset_id=asset_id,
        condition_id=condition_id,
        side=side,
        outcome=str(outcome),
        price=price,
        size=size,
    )


async def fetch_token_resolution(
    client: httpx.AsyncClient,
    token_id: str,
) -> tuple[float | None, datetime | None]:
    """Look up a token's final resolution price and resolution time.
    Returns (final_price_in_dollars, resolution_datetime).
    """
    if not token_id:
        return None, None
    try:
        # Hit the CLOB prices-history with interval=max — final point is post-resolution
        r = await client.get(
            "https://clob.polymarket.com/prices-history",
            params={"market": token_id, "interval": "max"},
            timeout=15.0,
        )
        if r.status_code != 200:
            return None, None
        history = r.json().get("history", []) or []
        if not history:
            return None, None
        last = history[-1]
        final_price = to_float(last.get("p"))
        ts_raw = last.get("t")
        final_ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc) if ts_raw else None
        # Resolution prices are 0 or 1 (sometimes 0.0001, ~0.999). Round.
        if final_price >= 0.95:
            return 1.0, final_ts
        if final_price <= 0.05:
            return 0.0, final_ts
        # In-between: market may not be fully resolved yet
        return None, final_ts
    except Exception:
        return None, None


async def main_async(
    *,
    classified_file: str,
    pages: int,
    size_per_trade: float,
    slippage_cents: float,
    output: str,
):
    console = Console()
    console.print("[bold]Directional whale copy-trade — deep backtest[/bold]\n")

    cls_path = Path(classified_file)
    if not cls_path.exists():
        console.print(f"[red]No {classified_file}. Run classify_and_backtest_whales.py first.[/red]")
        return
    with cls_path.open() as f:
        data = json.load(f)
    profiles = data.get("profiles", [])
    directional = [p for p in profiles if p.get("classification") == "directional"]
    if not directional:
        console.print("[red]No directional wallets in classified file.[/red]")
        return

    console.print(f"Loaded {len(directional)} directional wallets to backtest")
    for p in directional:
        addr = p["wallet"]
        console.print(f"  {addr[:10]}...{addr[-6:]}  30d PnL ${p['pnl_30d_usd']:+,.0f}  win {p['win_rate_30d']:.0%}")

    async with httpx.AsyncClient() as client:
        # Step 1: pull deep trade history per wallet
        console.print(f"\n[cyan]Pulling up to {pages * 500} trades per wallet...[/cyan]")
        wallet_trades: dict[str, list[Trade]] = {}
        for p in directional:
            addr = p["wallet"]
            raw = await fetch_user_trades_paginated(client, addr, pages=pages)
            parsed = [parse_trade(t) for t in raw]
            wallet_trades[addr] = [t for t in parsed if t is not None]
            console.print(f"  {addr[:10]}...{addr[-6:]}: {len(wallet_trades[addr])} trades parsed")

        # Step 2: collect all unique BUY token IDs
        all_tokens: set[str] = set()
        for trades in wallet_trades.values():
            for t in trades:
                if t.side == "BUY":
                    all_tokens.add(t.asset_id)
        console.print(f"\n[cyan]Resolving {len(all_tokens)} unique tokens...[/cyan]")

        # Step 3: resolve each token (price, resolution time)
        resolutions: dict[str, tuple[float | None, datetime | None]] = {}
        sem = asyncio.Semaphore(8)
        progress = 0

        async def resolve_one(tid: str):
            nonlocal progress
            async with sem:
                resolutions[tid] = await fetch_token_resolution(client, tid)
                progress += 1
                if progress % 50 == 0:
                    console.print(f"  ...resolved {progress}/{len(all_tokens)}")

        await asyncio.gather(*[resolve_one(t) for t in all_tokens])
        resolved_n = sum(1 for v in resolutions.values() if v[0] is not None)
        console.print(f"  Got resolution for {resolved_n}/{len(all_tokens)} tokens")

    if resolved_n == 0:
        console.print("\n[red]No tokens resolved. Likely all trades are on still-active markets.[/red]")
        console.print("Suggestion: increase --pages to walk further back, or the whales")
        console.print("only trade markets that haven't resolved yet (rare).")
        return

    # Step 4: simulate copy-trades at multiple lag scenarios
    slip = slippage_cents / 100.0
    lag_scenarios = [("30s", 30), ("5min", 300), ("30min", 1800)]

    summary_table = Table(title="Copy-trade results across lag scenarios")
    summary_table.add_column("Lag")
    summary_table.add_column("Trades", justify="right")
    summary_table.add_column("Wins", justify="right")
    summary_table.add_column("Win rate", justify="right")
    summary_table.add_column("Total PnL", justify="right")
    summary_table.add_column("Capital", justify="right")
    summary_table.add_column("ROI", justify="right")

    all_results: dict[str, list[CopyTrade]] = {}

    for lag_name, lag_sec in lag_scenarios:
        copy_trades: list[CopyTrade] = []
        for addr, trades in wallet_trades.items():
            for t in trades:
                if t.side != "BUY":
                    continue
                final_price, final_ts = resolutions.get(t.asset_id, (None, None))
                if final_price is None:
                    continue
                # If our entry would happen after resolution, skip
                our_entry = t.timestamp + timedelta(seconds=lag_sec)
                if final_ts and our_entry >= final_ts:
                    continue
                # Our fill price = whale's price + slippage, capped at 1.0
                our_price = min(0.99, t.price + slip)
                if our_price <= 0:
                    continue
                shares = size_per_trade / our_price
                payout = shares * final_price
                pnl = payout - size_per_trade
                copy_trades.append(CopyTrade(
                    wallet=addr,
                    market=t.market_title,
                    asset_id=t.asset_id,
                    whale_entry_ts=t.timestamp,
                    whale_price=t.price,
                    our_price=round(our_price, 4),
                    size_usd=size_per_trade,
                    final_price=final_price,
                    pnl_usd=round(pnl, 2),
                ))

        all_results[lag_name] = copy_trades
        if copy_trades:
            total_pnl = sum(c.pnl_usd for c in copy_trades)
            wins = sum(1 for c in copy_trades if c.pnl_usd > 0)
            deployed = sum(c.size_usd for c in copy_trades)
            roi = (total_pnl / deployed * 100) if deployed else 0
            color = "green" if roi > 0 else "red"
            summary_table.add_row(
                lag_name,
                str(len(copy_trades)),
                str(wins),
                f"{wins/len(copy_trades)*100:.1f}%",
                f"[{color}]${total_pnl:+,.2f}[/{color}]",
                f"${deployed:,.0f}",
                f"[{color}]{roi:+.2f}%[/{color}]",
            )
        else:
            summary_table.add_row(lag_name, "0", "--", "--", "--", "--", "--")

    console.print(summary_table)

    # Per-wallet detail at the 30s lag scenario
    if all_results.get("30s"):
        per_wallet: dict[str, list[CopyTrade]] = defaultdict(list)
        for c in all_results["30s"]:
            per_wallet[c.wallet].append(c)
        pw_table = Table(title="Per-wallet performance @ 30s lag")
        pw_table.add_column("Wallet")
        pw_table.add_column("Trades", justify="right")
        pw_table.add_column("PnL", justify="right")
        pw_table.add_column("Win%", justify="right")
        pw_table.add_column("Best", justify="right")
        pw_table.add_column("Worst", justify="right")
        for addr, ts in sorted(per_wallet.items(), key=lambda x: -sum(c.pnl_usd for c in x[1])):
            pnl = sum(c.pnl_usd for c in ts)
            wins = sum(1 for c in ts if c.pnl_usd > 0)
            best = max(c.pnl_usd for c in ts)
            worst = min(c.pnl_usd for c in ts)
            color = "green" if pnl >= 0 else "red"
            pw_table.add_row(
                addr[:10] + "..." + addr[-6:],
                str(len(ts)),
                f"[{color}]${pnl:+,.2f}[/{color}]",
                f"{wins/len(ts)*100:.0f}%",
                f"${best:+,.2f}",
                f"${worst:+,.2f}",
            )
        console.print(pw_table)

    # Distribution of trade prices — tells us where the edge comes from
    if all_results.get("30s"):
        buckets = {"0-0.20": 0, "0.20-0.40": 0, "0.40-0.60": 0, "0.60-0.80": 0, "0.80-0.95": 0, "0.95+": 0}
        bucket_pnl = {k: 0.0 for k in buckets}
        for c in all_results["30s"]:
            p = c.whale_price
            if p < 0.20:
                k = "0-0.20"
            elif p < 0.40:
                k = "0.20-0.40"
            elif p < 0.60:
                k = "0.40-0.60"
            elif p < 0.80:
                k = "0.60-0.80"
            elif p < 0.95:
                k = "0.80-0.95"
            else:
                k = "0.95+"
            buckets[k] += 1
            bucket_pnl[k] += c.pnl_usd
        bk_table = Table(title="PnL by entry-price bucket @ 30s lag (where does the edge come from?)")
        bk_table.add_column("Entry price")
        bk_table.add_column("Trades", justify="right")
        bk_table.add_column("PnL", justify="right")
        bk_table.add_column("Avg PnL/trade", justify="right")
        for k in ["0-0.20", "0.20-0.40", "0.40-0.60", "0.60-0.80", "0.80-0.95", "0.95+"]:
            n = buckets[k]
            pnl = bucket_pnl[k]
            avg = (pnl / n) if n else 0
            color = "green" if pnl >= 0 else "red"
            bk_table.add_row(
                k, str(n),
                f"[{color}]${pnl:+,.2f}[/{color}]",
                f"${avg:+,.2f}",
            )
        console.print(bk_table)

    # Save
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "size_per_trade_usd": size_per_trade,
            "slippage_cents": slippage_cents,
            "results_by_lag": {
                lag_name: [
                    {**asdict(c),
                     "whale_entry_ts": c.whale_entry_ts.isoformat()}
                    for c in trades
                ]
                for lag_name, trades in all_results.items()
            },
        }, f, indent=2, default=str)
    console.print(f"\nSaved {output}")


@click.command()
@click.option("--classified-file", default="data/whales_classified.json")
@click.option("--pages", type=int, default=5,
              help="Pages of trade history per wallet (500 trades/page).")
@click.option("--size-per-trade", type=float, default=25.0)
@click.option("--slippage-cents", type=float, default=1.0)
@click.option("--output", default="data/copy_trade_backtest.json")
def main(classified_file, pages, size_per_trade, slippage_cents, output):
    asyncio.run(main_async(
        classified_file=classified_file,
        pages=pages,
        size_per_trade=size_per_trade,
        slippage_cents=slippage_cents,
        output=output,
    ))


if __name__ == "__main__":
    main()
