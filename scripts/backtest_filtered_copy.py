"""Filtered copy-trade backtest: only mirror BUYs above a price threshold.

Hypothesis: the directional whales' edge lives in their high-conviction entries
(price >= 0.60). The longshot bets (price < 0.60) lose us money because we
can't replicate their sizing strategy. Filter those out and see what's left.
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
    asset_id: str
    condition_id: str
    side: str
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


async def fetch_user_trades_paginated(client, wallet, *, pages=5, page_size=500):
    all_trades = []
    for page in range(pages):
        try:
            r = await client.get(
                f"{DATA_API}/trades",
                params={"user": wallet, "limit": page_size, "offset": page * page_size},
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
                break
        except Exception:
            break
    return all_trades


def parse_trade(raw):
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
    return Trade(
        timestamp=ts,
        market_title=str(raw.get("title") or raw.get("question") or "?")[:80],
        asset_id=asset_id,
        condition_id=str(raw.get("conditionId") or ""),
        side=side,
        price=price,
        size=size,
    )


async def fetch_token_resolution(client, token_id):
    if not token_id:
        return None, None
    try:
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
        if final_price >= 0.95:
            return 1.0, final_ts
        if final_price <= 0.05:
            return 0.0, final_ts
        return None, final_ts
    except Exception:
        return None, None


def simulate_trades(wallet_trades, resolutions, *, lag_sec, slip, size_usd, min_price, max_price, excluded_wallets):
    copy_trades = []
    for addr, trades in wallet_trades.items():
        if addr in excluded_wallets:
            continue
        for t in trades:
            if t.side != "BUY":
                continue
            if t.price < min_price or t.price > max_price:
                continue
            final_price, final_ts = resolutions.get(t.asset_id, (None, None))
            if final_price is None:
                continue
            our_entry = t.timestamp + timedelta(seconds=lag_sec)
            if final_ts and our_entry >= final_ts:
                continue
            our_price = min(0.99, t.price + slip)
            if our_price <= 0:
                continue
            shares = size_usd / our_price
            payout = shares * final_price
            pnl = payout - size_usd
            copy_trades.append(CopyTrade(
                wallet=addr,
                market=t.market_title,
                asset_id=t.asset_id,
                whale_entry_ts=t.timestamp,
                whale_price=t.price,
                our_price=round(our_price, 4),
                size_usd=size_usd,
                final_price=final_price,
                pnl_usd=round(pnl, 2),
            ))
    return copy_trades


async def main_async(*, classified_file, pages, size_per_trade, slippage_cents,
                     min_price, max_price, exclude_wallet, output):
    console = Console()
    console.print("[bold]Filtered directional copy-trade backtest[/bold]\n")
    console.print(f"  Entry price filter: {min_price:.2f} <= whale price <= {max_price:.2f}")
    if exclude_wallet:
        console.print(f"  Excluding wallets: {', '.join(exclude_wallet)}")
    console.print()

    cls_path = Path(classified_file)
    if not cls_path.exists():
        console.print(f"[red]No {classified_file}. Run prior scripts first.[/red]")
        return
    with cls_path.open() as f:
        data = json.load(f)
    profiles = data.get("profiles", [])
    directional = [p for p in profiles if p.get("classification") == "directional"]
    if not directional:
        console.print("[red]No directional wallets.[/red]")
        return

    console.print(f"Loaded {len(directional)} directional wallets")
    excluded_set = {w.lower() for w in (exclude_wallet or [])}

    async with httpx.AsyncClient() as client:
        # Pull deep trade history
        console.print(f"\n[cyan]Pulling up to {pages * 500} trades per wallet...[/cyan]")
        wallet_trades = {}
        for p in directional:
            addr = p["wallet"]
            raw = await fetch_user_trades_paginated(client, addr, pages=pages)
            parsed = [parse_trade(t) for t in raw]
            wallet_trades[addr] = [t for t in parsed if t is not None]
            mark = " [EXCLUDED]" if addr.lower() in excluded_set else ""
            console.print(f"  {addr[:10]}...{addr[-6:]}: {len(wallet_trades[addr])} trades{mark}")

        # Collect tokens (only from non-excluded wallets, within price filter)
        all_tokens = set()
        for addr, trades in wallet_trades.items():
            if addr.lower() in excluded_set:
                continue
            for t in trades:
                if t.side == "BUY" and min_price <= t.price <= max_price:
                    all_tokens.add(t.asset_id)
        console.print(f"\n[cyan]Resolving {len(all_tokens)} unique tokens (filtered)...[/cyan]")

        resolutions = {}
        sem = asyncio.Semaphore(8)
        progress = 0

        async def resolve_one(tid):
            nonlocal progress
            async with sem:
                resolutions[tid] = await fetch_token_resolution(client, tid)
                progress += 1
                if progress % 50 == 0:
                    console.print(f"  ...{progress}/{len(all_tokens)}")

        await asyncio.gather(*[resolve_one(t) for t in all_tokens])
        resolved_n = sum(1 for v in resolutions.values() if v[0] is not None)
        console.print(f"  Got {resolved_n}/{len(all_tokens)} resolutions")

    if resolved_n == 0:
        console.print("\n[red]No resolutions found.[/red]")
        return

    slip = slippage_cents / 100.0

    # Run three lags
    summary_table = Table(title=f"Filtered results: price >= {min_price:.2f}, "
                          f"{'excluded ' + ','.join(exclude_wallet) if exclude_wallet else 'no wallets excluded'}")
    summary_table.add_column("Lag")
    summary_table.add_column("Trades", justify="right")
    summary_table.add_column("Win rate", justify="right")
    summary_table.add_column("Total PnL", justify="right")
    summary_table.add_column("Capital", justify="right")
    summary_table.add_column("ROI", justify="right")

    all_results = {}
    for lag_name, lag_sec in [("30s", 30), ("5min", 300), ("30min", 1800)]:
        trades_sim = simulate_trades(
            wallet_trades, resolutions,
            lag_sec=lag_sec, slip=slip, size_usd=size_per_trade,
            min_price=min_price, max_price=max_price,
            excluded_wallets=excluded_set,
        )
        all_results[lag_name] = trades_sim
        if trades_sim:
            pnl = sum(c.pnl_usd for c in trades_sim)
            wins = sum(1 for c in trades_sim if c.pnl_usd > 0)
            deployed = sum(c.size_usd for c in trades_sim)
            roi = (pnl / deployed * 100) if deployed else 0
            color = "green" if roi > 0 else "red"
            summary_table.add_row(
                lag_name, str(len(trades_sim)),
                f"{wins/len(trades_sim)*100:.1f}%",
                f"[{color}]${pnl:+,.2f}[/{color}]",
                f"${deployed:,.0f}",
                f"[{color}]{roi:+.2f}%[/{color}]",
            )
        else:
            summary_table.add_row(lag_name, "0", "--", "--", "--", "--")
    console.print(summary_table)

    # Per-wallet at 30s
    if all_results.get("30s"):
        per_wallet = defaultdict(list)
        for c in all_results["30s"]:
            per_wallet[c.wallet].append(c)
        pw_table = Table(title="Per-wallet @ 30s lag (filtered)")
        pw_table.add_column("Wallet")
        pw_table.add_column("Trades", justify="right")
        pw_table.add_column("PnL", justify="right")
        pw_table.add_column("Win%", justify="right")
        pw_table.add_column("Avg/trade", justify="right")
        for addr, ts in sorted(per_wallet.items(), key=lambda x: -sum(c.pnl_usd for c in x[1])):
            pnl = sum(c.pnl_usd for c in ts)
            wins = sum(1 for c in ts if c.pnl_usd > 0)
            avg = pnl / len(ts)
            color = "green" if pnl >= 0 else "red"
            pw_table.add_row(
                addr[:10] + "..." + addr[-6:],
                str(len(ts)),
                f"[{color}]${pnl:+,.2f}[/{color}]",
                f"{wins/len(ts)*100:.0f}%",
                f"${avg:+,.2f}",
            )
        console.print(pw_table)

    # Sub-bucket within filter, so we can see if 0.60-0.80 vs 0.80+ matters
    if all_results.get("30s"):
        buckets = {
            "{:.2f}-{:.2f}".format(min_price, min(min_price + 0.10, max_price)): (min_price, min_price + 0.10),
            "{:.2f}-{:.2f}".format(min_price + 0.10, min(min_price + 0.20, max_price)): (min_price + 0.10, min_price + 0.20),
            "{:.2f}-{:.2f}".format(min_price + 0.20, min(min_price + 0.35, max_price)): (min_price + 0.20, min_price + 0.35),
            "{:.2f}+".format(min_price + 0.35): (min_price + 0.35, max_price + 0.01),
        }
        bucket_pnl = {k: [0.0, 0] for k in buckets}
        for c in all_results["30s"]:
            for k, (lo, hi) in buckets.items():
                if lo <= c.whale_price < hi:
                    bucket_pnl[k][0] += c.pnl_usd
                    bucket_pnl[k][1] += 1
                    break
        bk_table = Table(title="Sub-buckets within filter")
        bk_table.add_column("Price range")
        bk_table.add_column("Trades", justify="right")
        bk_table.add_column("PnL", justify="right")
        bk_table.add_column("Avg/trade", justify="right")
        for k, (pnl, n) in bucket_pnl.items():
            if n == 0:
                continue
            avg = pnl / n
            color = "green" if pnl >= 0 else "red"
            bk_table.add_row(k, str(n),
                             f"[{color}]${pnl:+,.2f}[/{color}]",
                             f"${avg:+,.2f}")
        console.print(bk_table)

    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "filter": {"min_price": min_price, "max_price": max_price,
                       "excluded": list(excluded_set)},
            "size_per_trade_usd": size_per_trade,
            "slippage_cents": slippage_cents,
            "results_by_lag": {
                lag: [{**asdict(c), "whale_entry_ts": c.whale_entry_ts.isoformat()}
                      for c in trades]
                for lag, trades in all_results.items()
            },
        }, f, indent=2, default=str)
    console.print(f"\nSaved {output}")


@click.command()
@click.option("--classified-file", default="data/whales_classified.json")
@click.option("--pages", type=int, default=5)
@click.option("--size-per-trade", type=float, default=25.0)
@click.option("--slippage-cents", type=float, default=1.0)
@click.option("--min-price", type=float, default=0.60,
              help="Only mirror BUYs at or above this price.")
@click.option("--max-price", type=float, default=0.95,
              help="Skip near-certain markets where edge is dust.")
@click.option("--exclude-wallet", multiple=True,
              help="Wallet addresses to exclude (can repeat). Use for known losers.")
@click.option("--output", default="data/copy_trade_filtered.json")
def main(classified_file, pages, size_per_trade, slippage_cents,
         min_price, max_price, exclude_wallet, output):
    asyncio.run(main_async(
        classified_file=classified_file,
        pages=pages,
        size_per_trade=size_per_trade,
        slippage_cents=slippage_cents,
        min_price=min_price,
        max_price=max_price,
        exclude_wallet=list(exclude_wallet),
        output=output,
    ))


if __name__ == "__main__":
    main()
