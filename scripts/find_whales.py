"""Discover potentially profitable Polymarket wallets via the Data API."""
from __future__ import annotations

import asyncio
import json
import sys
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


@dataclass
class WhaleCandidate:
    wallet: str
    trade_count_30d: int
    trade_count_90d: int
    pnl_30d_usd: float
    pnl_90d_usd: float
    win_rate_30d: float
    win_rate_90d: float
    total_volume_usd: float
    last_trade_at: str
    sample_markets: list[str]

    def score(self) -> float:
        if self.trade_count_30d < 5:
            return -1e9
        return (
            self.pnl_30d_usd * 1.5
            + self.pnl_90d_usd * 0.5
            + self.trade_count_30d * 0.1
        )


def parse_ts(value) -> datetime | None:
    """Parse anything Polymarket might give us into a UTC-aware datetime."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        s = str(value)
        if s.isdigit():
            return datetime.fromtimestamp(int(s), tz=timezone.utc)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        return None


async def fetch_recent_active_markets(client: httpx.AsyncClient, limit: int) -> list[dict]:
    r = await client.get(
        f"{GAMMA_API}/markets",
        params={
            "closed": "false",
            "active": "true",
            "limit": limit,
            "order": "volume24hr",
            "ascending": "false",
        },
        timeout=20.0,
    )
    r.raise_for_status()
    return r.json()


async def fetch_market_trades(client: httpx.AsyncClient, condition_id: str, limit: int) -> list[dict]:
    try:
        r = await client.get(
            f"{DATA_API}/trades",
            params={"market": condition_id, "limit": limit},
            timeout=20.0,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("trades", []) or []
    except Exception:
        return []


async def fetch_user_positions(client: httpx.AsyncClient, wallet: str) -> list[dict]:
    try:
        r = await client.get(
            f"{DATA_API}/positions",
            params={"user": wallet, "limit": 500},
            timeout=20.0,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("positions", []) or []
    except Exception:
        return []


async def discover_wallet_universe(
    client: httpx.AsyncClient,
    *,
    market_sample: int,
    trades_per_market: int,
    console: Console,
) -> list[str]:
    console.print(f"[cyan]Pulling top {market_sample} active markets by 24h volume...[/cyan]")
    markets = await fetch_recent_active_markets(client, limit=market_sample)
    console.print(f"  Got {len(markets)} markets. Walking trades on each...")

    # Track wallets with their trade frequency so we can prioritize
    wallet_counts: dict[str, int] = {}
    sem = asyncio.Semaphore(8)

    async def walk_market(m: dict):
        async with sem:
            cond_id = m.get("conditionId") or m.get("id")
            if not cond_id:
                return
            trades = await fetch_market_trades(client, str(cond_id), limit=trades_per_market)
            for t in trades:
                addr = t.get("proxyWallet") or t.get("user") or t.get("taker") or t.get("maker")
                if addr:
                    a = str(addr).lower()
                    wallet_counts[a] = wallet_counts.get(a, 0) + 1

    await asyncio.gather(*[walk_market(m) for m in markets])
    console.print(f"  Found {len(wallet_counts)} unique wallets across recent trades")

    # Return wallets sorted by trade frequency (active wallets first)
    sorted_wallets = sorted(wallet_counts.items(), key=lambda x: x[1], reverse=True)
    return [w for w, _ in sorted_wallets]


def compute_wallet_stats(positions: list[dict], wallet: str) -> WhaleCandidate | None:
    now = datetime.now(timezone.utc)
    cutoff_30d = now - timedelta(days=30)
    cutoff_90d = now - timedelta(days=90)

    pnl_30d = 0.0
    pnl_90d = 0.0
    trades_30d = 0
    trades_90d = 0
    wins_30d = 0
    wins_90d = 0
    volume_total = 0.0
    last_trade_dt: datetime | None = None
    sample_markets: list[str] = []

    for pos in positions:
        realized = pos.get("realizedPnl") or pos.get("realized_pnl") or 0
        try:
            realized = float(realized)
        except (TypeError, ValueError):
            continue

        if realized == 0:
            continue

        ts = (
            parse_ts(pos.get("endDate"))
            or parse_ts(pos.get("redeemed_at"))
            or parse_ts(pos.get("updatedAt"))
            or parse_ts(pos.get("created_at"))
        )

        size = pos.get("size") or pos.get("initialValue") or 0
        try:
            size = float(size)
        except (TypeError, ValueError):
            size = 0.0
        volume_total += abs(size)

        question = pos.get("title") or pos.get("question") or pos.get("slug") or "?"
        if len(sample_markets) < 5 and question != "?":
            sample_markets.append(str(question)[:80])

        if ts:
            if last_trade_dt is None or ts > last_trade_dt:
                last_trade_dt = ts
            if ts > cutoff_90d:
                pnl_90d += realized
                trades_90d += 1
                if realized > 0:
                    wins_90d += 1
                if ts > cutoff_30d:
                    pnl_30d += realized
                    trades_30d += 1
                    if realized > 0:
                        wins_30d += 1
        else:
            pnl_90d += realized
            trades_90d += 1
            if realized > 0:
                wins_90d += 1

    if trades_90d == 0:
        return None

    return WhaleCandidate(
        wallet=wallet,
        trade_count_30d=trades_30d,
        trade_count_90d=trades_90d,
        pnl_30d_usd=round(pnl_30d, 2),
        pnl_90d_usd=round(pnl_90d, 2),
        win_rate_30d=round(wins_30d / trades_30d, 3) if trades_30d else 0.0,
        win_rate_90d=round(wins_90d / trades_90d, 3) if trades_90d else 0.0,
        total_volume_usd=round(volume_total, 2),
        last_trade_at=last_trade_dt.isoformat() if last_trade_dt else "",
        sample_markets=sample_markets,
    )


async def evaluate_wallets(
    client: httpx.AsyncClient,
    wallets: list[str],
    *,
    console: Console,
    debug_dump_path: Path,
) -> list[WhaleCandidate]:
    console.print(f"[cyan]Evaluating {len(wallets)} candidate wallets...[/cyan]")
    sem = asyncio.Semaphore(10)
    candidates: list[WhaleCandidate] = []
    progress_n = 0
    debug_dumped = False

    async def evaluate_one(addr: str):
        nonlocal progress_n, debug_dumped
        async with sem:
            positions = await fetch_user_positions(client, addr)
            progress_n += 1
            if progress_n == 1 and positions and not debug_dumped:
                # Dump the first wallet's raw position data so we can see the schema
                debug_dumped = True
                debug_dump_path.parent.mkdir(parents=True, exist_ok=True)
                with debug_dump_path.open("w") as f:
                    json.dump({"wallet": addr, "positions_sample": positions[:3]}, f, indent=2, default=str)
                console.print(f"  [dim]Schema sample saved to {debug_dump_path}[/dim]")
            if progress_n % 50 == 0:
                console.print(f"  ...evaluated {progress_n}/{len(wallets)} ({len(candidates)} candidates so far)")
            if not positions:
                return
            stats = compute_wallet_stats(positions, addr)
            if stats:
                candidates.append(stats)

    await asyncio.gather(*[evaluate_one(w) for w in wallets])
    return candidates


@click.command()
@click.option("--market-sample", type=int, default=50)
@click.option("--trades-per-market", type=int, default=200)
@click.option("--max-wallets", type=int, default=500,
              help="Max wallets to deeply evaluate (sorted by trade activity).")
@click.option("--top-n", type=int, default=20)
@click.option("--min-trades-30d", type=int, default=5)
@click.option("--min-pnl-90d", type=float, default=100.0)
@click.option("--output", type=str, default="data/whales.json")
def main(market_sample, trades_per_market, max_wallets, top_n, min_trades_30d, min_pnl_90d, output):
    asyncio.run(_run(
        market_sample=market_sample,
        trades_per_market=trades_per_market,
        max_wallets=max_wallets,
        top_n=top_n,
        min_trades_30d=min_trades_30d,
        min_pnl_90d=min_pnl_90d,
        output=output,
    ))


async def _run(*, market_sample, trades_per_market, max_wallets, top_n, min_trades_30d, min_pnl_90d, output):
    console = Console()
    console.print("[bold]Polymarket whale finder[/bold]")
    console.print(f"Sampling {market_sample} markets, evaluating top {max_wallets} active wallets.\n")

    debug_dump = Path("data/whale_debug_sample.json")

    async with httpx.AsyncClient() as client:
        wallets = await discover_wallet_universe(
            client,
            market_sample=market_sample,
            trades_per_market=trades_per_market,
            console=console,
        )
        if not wallets:
            console.print("[red]No wallets discovered. Check Data API access.[/red]")
            return

        # Cap to most-active wallets to keep this fast
        wallets = wallets[:max_wallets]
        console.print(f"  Evaluating top {len(wallets)} most-active wallets")

        candidates = await evaluate_wallets(client, wallets, console=console, debug_dump_path=debug_dump)

    qualified = [
        c for c in candidates
        if c.trade_count_30d >= min_trades_30d
        and c.pnl_90d_usd >= min_pnl_90d
    ]
    qualified.sort(key=lambda c: c.score(), reverse=True)

    console.print(f"\n[bold]{len(candidates)} wallets returned position data[/bold]")
    console.print(f"[green]{len(qualified)} passed filters[/green] "
                  f"(min {min_trades_30d} trades 30d, min ${min_pnl_90d:.0f} 90d PnL)")

    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not qualified:
        console.print("\n[yellow]No wallets met thresholds. Saving diagnostic dump for inspection.[/yellow]")
        sorted_all = sorted(candidates, key=lambda x: x.pnl_90d_usd, reverse=True)
        with out_path.open("w") as f:
            json.dump({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "qualified_count": 0,
                "all_candidates_count": len(candidates),
                "top_50_by_pnl": [asdict(c) for c in sorted_all[:50]],
            }, f, indent=2)
        console.print(f"  Diagnostic saved: {output}")
        console.print(f"  Schema sample (first wallet's raw positions): {debug_dump}")
        console.print("\n  If you want, paste the top of {output} and {debug_dump} so we can")
        console.print("  see what fields the Data API is actually returning.")
        return

    table = Table(title=f"Top {min(top_n, len(qualified))} whale candidates")
    table.add_column("Wallet", overflow="fold")
    table.add_column("30d PnL", justify="right")
    table.add_column("90d PnL", justify="right")
    table.add_column("30d Trades", justify="right")
    table.add_column("30d Win%", justify="right")
    table.add_column("Volume", justify="right")
    for c in qualified[:top_n]:
        color = "green" if c.pnl_30d_usd >= 0 else "red"
        table.add_row(
            c.wallet[:10] + "..." + c.wallet[-6:],
            f"[{color}]${c.pnl_30d_usd:+,.0f}[/{color}]",
            f"${c.pnl_90d_usd:+,.0f}",
            str(c.trade_count_30d),
            f"{c.win_rate_30d * 100:.0f}%",
            f"${c.total_volume_usd:,.0f}",
        )
    console.print(table)

    with out_path.open("w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "qualified_count": len(qualified),
            "qualified": [asdict(c) for c in qualified],
        }, f, indent=2)
    console.print(f"\nFull list saved to {output}")
    console.print("\nNext: review candidates, then we wire copy-trade backtest against them.")


if __name__ == "__main__":
    main()
