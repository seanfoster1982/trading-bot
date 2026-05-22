"""Classify whale wallets and backtest copy-trading the directional ones.

Reads ``data/whales.json`` (from ``find_whales.py``), pulls each wallet's trade
history from the Polymarket Data API, classifies behavior, then simulates
following directional traders' BUY entries at configurable lag and slippage.

Trade history is capped at ``limit=500`` per wallet (Data API default/max in
practice). There is no cursor pagination in this script; raise ``limit`` only
if the API begins supporting higher caps.

``--min-market-volume``: when > 0, BUYs are skipped unless Gamma returns a
numeric volume field for that condition id (``volumeNum``, ``volume``, or
``volume24hr``). If Gamma returns no market row or no volume, the trade is
skipped (conservative). Trade payloads are not assumed to carry reliable total
market volume.
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
    """Parse Polymarket timestamps to UTC-aware datetimes (naive → UTC)."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        s = str(value)
        if s.replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        return None


def to_float(v, default=0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def gamma_market_volume_usd(m: dict) -> float | None:
    """Best-effort total / rolling volume in USD from a Gamma market object."""
    for key in ("volumeNum", "volume", "volume24hr", "volume24hrClob"):
        val = m.get(key)
        if val is None:
            continue
        try:
            f = float(val)
            if f > 0:
                return f
        except (TypeError, ValueError):
            continue
    return None


@dataclass
class Trade:
    timestamp: datetime
    market: str
    market_id: str
    side: str  # BUY or SELL
    price: float
    size: float
    is_taker: bool


@dataclass
class WalletProfile:
    wallet: str
    classification: str  # market_maker | arbitrageur | directional | mixed | insufficient_data
    classification_reason: str
    trade_count: int
    unique_markets: int
    avg_trades_per_market: float
    pct_taker: float
    avg_hold_minutes: float
    same_market_quick_flips: int  # bought and sold same market within 60s
    pnl_30d_usd: float
    pnl_90d_usd: float
    win_rate_30d: float


@dataclass
class CopyTrade:
    wallet: str
    market: str
    whale_entry_ts: datetime
    whale_price: float
    our_entry_ts: datetime
    our_price: float
    size_usd: float
    resolved_to: int | None  # 1 if YES won, 0 if NO, None if unknown
    pnl_usd: float | None


async def fetch_user_trades(client: httpx.AsyncClient, wallet: str, limit: int = 500) -> list[dict]:
    """Pull a wallet's recent trades (see module docstring for pagination cap)."""
    try:
        r = await client.get(
            f"{DATA_API}/trades",
            params={"user": wallet, "limit": limit},
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


def _condition_id_variants(condition_id: str) -> list[str]:
    cid = (condition_id or "").strip()
    if not cid:
        return []
    out: list[str] = [cid]
    lower = cid.lower()
    if lower.startswith("0x") and len(cid) == 66:
        out.append(cid[2:])
    elif len(cid) == 64 and all(c in "0123456789abcdefABCDEF" for c in cid):
        out.append("0x" + lower)
    # de-dup preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


async def fetch_gamma_market_meta(
    client: httpx.AsyncClient,
    condition_id: str,
) -> tuple[dict | None, int | None, float | None]:
    """Fetch first Gamma market row for ``condition_id``; return (raw, yes_wins, volume_usd).

    Tries ``condition_ids`` (documented), then ``condition_id`` (legacy), and
    normalized id variants (with/without ``0x``).
    """
    if not condition_id:
        return None, None, None

    async def try_params(params: dict) -> list[dict] | None:
        try:
            r = await client.get(f"{GAMMA_API}/markets", params=params, timeout=15.0)
            if r.status_code != 200:
                return None
            data = r.json()
            if isinstance(data, list) and data:
                return data
            if isinstance(data, dict) and data:
                return [data]
        except Exception:
            return None
        return None

    for variant in _condition_id_variants(str(condition_id)):
        for key in ("condition_ids", "condition_id"):
            rows = await try_params({key: variant})
            if rows:
                m = rows[0]
                vol = gamma_market_volume_usd(m)
                resolution: int | None = None
                if m.get("closed"):
                    prices = m.get("outcomePrices")
                    if isinstance(prices, str):
                        try:
                            prices = json.loads(prices)
                        except json.JSONDecodeError:
                            prices = None
                    if prices and len(prices) >= 1:
                        try:
                            resolution = 1 if float(prices[0]) >= 0.5 else 0
                        except (TypeError, ValueError):
                            resolution = None
                return m, resolution, vol
    return None, None, None


def parse_trade(raw: dict, wallet: str) -> Trade | None:
    """Convert a raw trade dict into our Trade dataclass. Tolerates schema variation."""
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
    market = raw.get("title") or raw.get("question") or raw.get("slug") or "?"
    market_id = str(raw.get("conditionId") or raw.get("market") or "")
    maker = (raw.get("maker") or "").lower()
    is_taker = maker != wallet.lower() if maker else True
    return Trade(
        timestamp=ts,
        market=str(market)[:80],
        market_id=market_id,
        side=side,
        price=price,
        size=size,
        is_taker=is_taker,
    )


def classify_wallet(wallet: str, trades: list[Trade], whale_meta: dict) -> WalletProfile:
    if len(trades) < 10:
        return WalletProfile(
            wallet=wallet,
            classification="insufficient_data",
            classification_reason=f"only {len(trades)} trades fetched",
            trade_count=len(trades),
            unique_markets=0,
            avg_trades_per_market=0.0,
            pct_taker=0.0,
            avg_hold_minutes=0.0,
            same_market_quick_flips=0,
            pnl_30d_usd=to_float(whale_meta.get("pnl_30d_usd")),
            pnl_90d_usd=to_float(whale_meta.get("pnl_90d_usd")),
            win_rate_30d=to_float(whale_meta.get("win_rate_30d")),
        )

    by_market: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        by_market[t.market_id].append(t)
    for mid in by_market:
        by_market[mid].sort(key=lambda t: t.timestamp)

    quick_flips = 0
    hold_durations: list[float] = []
    for _mid, ts_list in by_market.items():
        last_buy: Trade | None = None
        for tr in ts_list:
            if tr.side == "BUY":
                last_buy = tr
            elif tr.side == "SELL" and last_buy:
                hold_min = (tr.timestamp - last_buy.timestamp).total_seconds() / 60
                hold_durations.append(hold_min)
                if hold_min < 1.0:
                    quick_flips += 1
                last_buy = None

    unique_markets = len(by_market)
    avg_per_market = len(trades) / unique_markets if unique_markets else 0
    pct_taker = sum(1 for t in trades if t.is_taker) / len(trades)
    avg_hold = sum(hold_durations) / len(hold_durations) if hold_durations else float("inf")
    win_rate_30d = to_float(whale_meta.get("win_rate_30d"))

    classification = "mixed"
    reason = ""

    if quick_flips >= 5 and avg_per_market >= 4 and win_rate_30d >= 0.85:
        classification = "market_maker"
        reason = f"{quick_flips} sub-1min flips, {avg_per_market:.1f} trades/market, {win_rate_30d:.0%} win rate"
    elif win_rate_30d >= 0.90 and unique_markets >= 30:
        classification = "arbitrageur"
        reason = f"{win_rate_30d:.0%} win rate across {unique_markets} markets, low quick-flip"
    elif 0.45 <= win_rate_30d <= 0.80 and avg_per_market <= 3.0 and avg_hold > 60:
        classification = "directional"
        reason = f"{win_rate_30d:.0%} win rate, {avg_per_market:.1f} trades/market, avg hold {avg_hold:.0f}min"
    elif win_rate_30d < 0.45 and to_float(whale_meta.get("pnl_30d_usd")) > 1000:
        classification = "directional"
        reason = f"profitable despite {win_rate_30d:.0%} win rate (big winners > many losers)"
    else:
        reason = (
            f"win_rate={win_rate_30d:.0%}, trades/market={avg_per_market:.1f}, "
            f"flips={quick_flips}, avg_hold={avg_hold:.0f}min"
        )

    return WalletProfile(
        wallet=wallet,
        classification=classification,
        classification_reason=reason,
        trade_count=len(trades),
        unique_markets=unique_markets,
        avg_trades_per_market=round(avg_per_market, 2),
        pct_taker=round(pct_taker, 3),
        avg_hold_minutes=round(avg_hold, 1) if avg_hold != float("inf") else -1.0,
        same_market_quick_flips=quick_flips,
        pnl_30d_usd=to_float(whale_meta.get("pnl_30d_usd")),
        pnl_90d_usd=to_float(whale_meta.get("pnl_90d_usd")),
        win_rate_30d=win_rate_30d,
    )


async def backtest_copy_directional(
    client: httpx.AsyncClient,
    profiles: list[WalletProfile],
    wallet_trades: dict[str, list[Trade]],
    *,
    lag_seconds: int,
    slippage_cents: float,
    size_per_trade_usd: float,
    min_market_volume_usd: float,
    console: Console,
) -> list[CopyTrade]:
    """Replay each directional wallet's BUY entries with our mirror."""
    directional = [p for p in profiles if p.classification == "directional"]
    if not directional:
        console.print("[yellow]No directional wallets to backtest.[/yellow]")
        return []

    console.print(f"\n[cyan]Backtesting copy-trade against {len(directional)} directional wallets[/cyan]")
    console.print(f"  Lag: {lag_seconds}s  Slippage: +{slippage_cents:.1f}c  Size: ${size_per_trade_usd:.0f}/trade")
    if min_market_volume_usd > 0:
        console.print(
            f"  Min market volume: ${min_market_volume_usd:,.0f} (from Gamma; BUYs skipped if volume unknown)"
        )

    all_market_ids: set[str] = set()
    for p in directional:
        for t in wallet_trades.get(p.wallet, []):
            if t.side == "BUY" and t.market_id:
                all_market_ids.add(t.market_id)

    console.print(f"  Resolving {len(all_market_ids)} unique markets...")
    resolutions: dict[str, int | None] = {}
    volumes: dict[str, float | None] = {}
    sem = asyncio.Semaphore(8)

    async def resolve_one(mid: str) -> None:
        async with sem:
            _raw, res, vol = await fetch_gamma_market_meta(client, mid)
            resolutions[mid] = res
            volumes[mid] = vol

    await asyncio.gather(*[resolve_one(mid) for mid in all_market_ids])
    resolved_count = sum(1 for v in resolutions.values() if v is not None)
    console.print(f"  Got resolution for {resolved_count}/{len(all_market_ids)} markets")
    if resolved_count == 0 and all_market_ids:
        console.print(
            "  [dim]Gamma reports no closed markets in this sample — "
            "mirrored PnL needs resolved (closed) markets.[/dim]"
        )

    copy_trades: list[CopyTrade] = []
    slippage = slippage_cents / 100.0
    skipped_volume = 0

    for prof in directional:
        for t in wallet_trades.get(prof.wallet, []):
            if t.side != "BUY":
                continue
            resolution = resolutions.get(t.market_id)
            if resolution is None:
                continue

            if min_market_volume_usd > 0:
                v = volumes.get(t.market_id)
                if v is None or v < min_market_volume_usd:
                    skipped_volume += 1
                    continue

            our_entry = t.timestamp + timedelta(seconds=lag_seconds)
            our_price = min(1.0, t.price + slippage)
            shares = size_per_trade_usd / our_price if our_price > 0 else 0
            payout = shares * resolution
            pnl = payout - size_per_trade_usd

            copy_trades.append(
                CopyTrade(
                    wallet=prof.wallet,
                    market=t.market[:60],
                    whale_entry_ts=t.timestamp,
                    whale_price=t.price,
                    our_entry_ts=our_entry,
                    our_price=round(our_price, 4),
                    size_usd=size_per_trade_usd,
                    resolved_to=resolution,
                    pnl_usd=round(pnl, 2),
                )
            )

    if min_market_volume_usd > 0 and skipped_volume:
        console.print(f"  Skipped {skipped_volume} BUY(s) below volume threshold or missing Gamma volume")

    return copy_trades


@click.command()
@click.option("--whales-file", default="data/whales.json")
@click.option("--lag-seconds", type=int, default=60, help="How long after the whale we mirror.")
@click.option("--slippage-cents", type=float, default=1.0, help="Cents we pay above the whale's fill price.")
@click.option("--size-per-trade", type=float, default=25.0)
@click.option(
    "--min-market-volume",
    type=float,
    default=0.0,
    help="Minimum Gamma-reported market volume (USD) to include a mirrored BUY; 0 disables.",
)
@click.option("--output", default="data/whales_classified.json")
def main(whales_file, lag_seconds, slippage_cents, size_per_trade, min_market_volume, output):
    asyncio.run(
        _run(
            whales_file=whales_file,
            lag_seconds=lag_seconds,
            slippage_cents=slippage_cents,
            size_per_trade=size_per_trade,
            min_market_volume=min_market_volume,
            output=output,
        )
    )


async def _run(*, whales_file, lag_seconds, slippage_cents, size_per_trade, min_market_volume, output):
    console = Console()
    console.print("[bold]Whale classifier + copy-trade backtest[/bold]\n")

    whales_path = Path(whales_file)
    if not whales_path.exists():
        console.print(f"[red]No {whales_file} found. Run scripts/find_whales.py first.[/red]")
        return

    with whales_path.open(encoding="utf-8") as f:
        whales_data = json.load(f)

    whale_list = whales_data.get("qualified") or whales_data.get("top_50_by_pnl") or []
    if not whale_list:
        console.print("[red]whales.json has no qualified candidates.[/red]")
        return

    console.print(f"Loaded {len(whale_list)} whales from {whales_file}")

    console.print("\n[cyan]Fetching trade history per wallet...[/cyan]")
    wallet_trades: dict[str, list[Trade]] = {}
    sem = asyncio.Semaphore(6)

    async with httpx.AsyncClient() as client:

        async def fetch_one(w: dict) -> None:
            addr = w["wallet"]
            async with sem:
                raw = await fetch_user_trades(client, addr, limit=500)
                trades = [parse_trade(t, addr) for t in raw]
                wallet_trades[addr] = [t for t in trades if t is not None]

        await asyncio.gather(*[fetch_one(w) for w in whale_list])

        total_trades = sum(len(v) for v in wallet_trades.values())
        console.print(f"  Got {total_trades} total trades across {len(wallet_trades)} wallets")

        console.print("\n[cyan]Classifying wallets...[/cyan]")
        profiles: list[WalletProfile] = []
        for w in whale_list:
            addr = w["wallet"]
            prof = classify_wallet(addr, wallet_trades.get(addr, []), w)
            profiles.append(prof)

        breakdown: dict[str, int] = defaultdict(int)
        for p in profiles:
            breakdown[p.classification] += 1

        bd_table = Table(title="Classification breakdown")
        bd_table.add_column("Type")
        bd_table.add_column("Count", justify="right")
        for kind in ["directional", "market_maker", "arbitrageur", "mixed", "insufficient_data"]:
            if breakdown[kind]:
                bd_table.add_row(kind, str(breakdown[kind]))
        console.print(bd_table)

        directional = [p for p in profiles if p.classification == "directional"]
        if directional:
            d_table = Table(title=f"{len(directional)} directional wallets")
            d_table.add_column("Wallet", overflow="fold")
            d_table.add_column("30d PnL", justify="right")
            d_table.add_column("Win%", justify="right")
            d_table.add_column("Markets", justify="right")
            d_table.add_column("Avg hold", justify="right")
            d_table.add_column("Reason", overflow="fold", max_width=50)
            for p in sorted(directional, key=lambda x: x.pnl_30d_usd, reverse=True):
                d_table.add_row(
                    p.wallet[:10] + "..." + p.wallet[-6:],
                    f"${p.pnl_30d_usd:+,.0f}",
                    f"{p.win_rate_30d:.0%}",
                    str(p.unique_markets),
                    f"{p.avg_hold_minutes:.0f}m" if p.avg_hold_minutes >= 0 else "--",
                    p.classification_reason,
                )
            console.print(d_table)

        copy_trades = await backtest_copy_directional(
            client,
            profiles,
            wallet_trades,
            lag_seconds=lag_seconds,
            slippage_cents=slippage_cents,
            size_per_trade_usd=size_per_trade,
            min_market_volume_usd=min_market_volume,
            console=console,
        )

    if not copy_trades:
        console.print(
            "\n[yellow]No copy trades simulated (no resolved markets among directional whales' BUYs, "
            "or all filtered).[/yellow]"
        )
    else:
        wins = [c for c in copy_trades if c.pnl_usd and c.pnl_usd > 0]
        losses = [c for c in copy_trades if c.pnl_usd and c.pnl_usd < 0]
        total_pnl = sum(c.pnl_usd or 0 for c in copy_trades)
        deployed = sum(c.size_usd for c in copy_trades)
        roi = (total_pnl / deployed * 100) if deployed else 0

        console.print("\n[bold cyan]Copy-trade backtest summary[/bold cyan]")
        console.print(f"  Mirror trades:    {len(copy_trades)}")
        console.print(f"  Wins / losses:    {len(wins)} / {len(losses)}")
        console.print(f"  Win rate:         {len(wins) / len(copy_trades) * 100:.1f}%")
        console.print(f"  Total PnL:        ${total_pnl:+,.2f}")
        console.print(f"  Capital deployed: ${deployed:,.2f}")
        console.print(f"  ROI on deployed:  {roi:+.2f}%")

        per_wallet: dict[str, list[CopyTrade]] = defaultdict(list)
        for c in copy_trades:
            per_wallet[c.wallet].append(c)

        pw_table = Table(title="Per-wallet performance")
        pw_table.add_column("Wallet", overflow="fold")
        pw_table.add_column("Trades", justify="right")
        pw_table.add_column("PnL", justify="right")
        pw_table.add_column("Win%", justify="right")
        for w, ts in sorted(per_wallet.items(), key=lambda x: -sum(c.pnl_usd or 0 for c in x[1])):
            wpnl = sum(c.pnl_usd or 0 for c in ts)
            wwins = sum(1 for c in ts if (c.pnl_usd or 0) > 0)
            color = "green" if wpnl >= 0 else "red"
            pw_table.add_row(
                w[:10] + "..." + w[-6:],
                str(len(ts)),
                f"[{color}]${wpnl:+,.2f}[/{color}]",
                f"{wwins / len(ts) * 100:.0f}%",
            )
        console.print(pw_table)

    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "lag_seconds": lag_seconds,
                "slippage_cents": slippage_cents,
                "size_per_trade_usd": size_per_trade,
                "min_market_volume_usd": min_market_volume,
                "profiles": [asdict(p) for p in profiles],
                "copy_trades": [
                    {
                        **asdict(c),
                        "whale_entry_ts": c.whale_entry_ts.isoformat(),
                        "our_entry_ts": c.our_entry_ts.isoformat(),
                    }
                    for c in copy_trades
                ],
            },
            f,
            indent=2,
            default=str,
        )
    console.print(f"\nSaved {output}")


if __name__ == "__main__":
    main()
