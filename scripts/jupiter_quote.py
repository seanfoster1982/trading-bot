"""Jupiter quote viewer - shows what trades would fill at given current liquidity.

Read-only. No signing. No wallet interaction.

For each pending BUY signal in the signals table:
  1. Query Jupiter /swap/v1/quote on lite-api.jup.ag (free tier, no auth)
  2. Show expected output amount + price impact + route
  3. Compare to strategy entry price - flag if slippage too high

Run with --test-size N to do synthetic quotes when no signals exist.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import click
import httpx
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = Path("data/memecoins.db")
JUPITER_QUOTE_API = "https://lite-api.jup.ag/swap/v1/quote"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DEFAULT_SLIPPAGE_BPS = 100  # 1.0% slippage tolerance


@dataclass
class QuoteResult:
    symbol: str
    address: str
    usdc_in: float
    tokens_out: float
    fill_price: float
    strategy_price: float
    price_diff_pct: float
    price_impact_pct: float
    route_count: int
    flagged: bool
    flag_reason: str


def get_pending_signals():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT s.id, s.symbol, s.address, s.action, s.strategy,
               s.entry_price, s.stop_loss, s.take_profit, s.position_size_usd,
               s.generated_at
        FROM signals s
        INNER JOIN (
            SELECT address, MAX(generated_at) as max_ts
            FROM signals
            WHERE action = 'BUY' AND executed = 0
            GROUP BY address
        ) latest ON s.address = latest.address AND s.generated_at = latest.max_ts
        ORDER BY s.generated_at DESC
    """).fetchall()
    conn.close()
    cols = ["id", "symbol", "address", "action", "strategy",
            "entry_price", "stop_loss", "take_profit", "position_size_usd",
            "generated_at"]
    return [dict(zip(cols, r)) for r in rows]


def get_token_decimals(address):
    # Real lookup via token_metadata module - hits cache first, Birdeye on miss
    from token_metadata import get_decimals
    d = get_decimals(address)
    if d is None:
        # Fallback only if metadata lookup completely failed - log it loudly
        print(f"WARN: no decimals found for {address}, falling back to 6")
        return 6
    return d


async def fetch_jupiter_quote(client, input_mint, output_mint, amount_atomic,
                               slippage_bps=DEFAULT_SLIPPAGE_BPS):
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_atomic),
        "slippageBps": str(slippage_bps),
        "swapMode": "ExactIn",
    }
    try:
        r = await client.get(JUPITER_QUOTE_API, params=params, timeout=20.0)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return r.json()
    except Exception as e:
        return {"error": f"Exception: {str(e)[:200]}"}


async def quote_signal(client, sig):
    usdc_decimals = 6
    usdc_atomic = int(sig["position_size_usd"] * (10 ** usdc_decimals))
    token_decimals = get_token_decimals(sig["address"])

    quote = await fetch_jupiter_quote(client, USDC_MINT, sig["address"], usdc_atomic)
    if not quote or quote.get("error"):
        return QuoteResult(
            symbol=sig["symbol"], address=sig["address"],
            usdc_in=sig["position_size_usd"], tokens_out=0,
            fill_price=0, strategy_price=sig["entry_price"],
            price_diff_pct=0, price_impact_pct=0, route_count=0,
            flagged=True,
            flag_reason=quote.get("error", "no quote returned") if quote else "no response",
        )

    out_amount_atomic = int(quote.get("outAmount", 0))
    tokens_out = out_amount_atomic / (10 ** token_decimals)
    fill_price = sig["position_size_usd"] / tokens_out if tokens_out > 0 else 0

    strategy_price = sig["entry_price"]
    price_diff_pct = ((fill_price - strategy_price) / strategy_price * 100) if strategy_price else 0
    price_impact_pct_raw = quote.get("priceImpactPct", "0")
    try:
        price_impact_pct = float(price_impact_pct_raw) * 100
    except (TypeError, ValueError):
        price_impact_pct = 0
    route_count = len(quote.get("routePlan", []))

    flagged = False
    flag_reason = ""
    if tokens_out <= 0:
        flagged = True
        flag_reason = "no liquidity for this trade size"
    elif abs(price_diff_pct) > 5:
        flagged = True
        flag_reason = f"fill price {price_diff_pct:+.1f}% from strategy price"
    elif price_impact_pct > 3:
        flagged = True
        flag_reason = f"price impact {price_impact_pct:.2f}% too high for size"

    return QuoteResult(
        symbol=sig["symbol"], address=sig["address"],
        usdc_in=sig["position_size_usd"], tokens_out=tokens_out,
        fill_price=fill_price, strategy_price=strategy_price,
        price_diff_pct=price_diff_pct, price_impact_pct=price_impact_pct,
        route_count=route_count, flagged=flagged, flag_reason=flag_reason,
    )


async def main_async(test_size_usd):
    console = Console()
    console.print("[bold]Jupiter Quote Viewer[/bold]")
    console.print("Read-only. No signing. No wallet interaction.\n")

    signals = get_pending_signals()

    if not signals and test_size_usd:
        size_msg = "$" + f"{test_size_usd}"
        console.print(f"[yellow]No pending BUY signals. Running synthetic quotes at {size_msg} per token.[/yellow]\n")
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT DISTINCT s.symbol, s.address
            FROM screened_tokens s
            INNER JOIN rug_reports r ON s.address = r.address
            WHERE r.rug_score < 25 AND r.freeze_authority_active = 0
            LIMIT 10
        """).fetchall()
        conn.close()
        signals = []
        for sym, addr in rows:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute("""
                SELECT close FROM indicators
                WHERE address = ? AND interval = '15m'
                ORDER BY timestamp DESC LIMIT 1
            """, (addr,)).fetchone()
            conn.close()
            ref_price = row[0] if row else 0
            signals.append({
                "id": -1, "symbol": sym, "address": addr,
                "action": "TEST", "strategy": "synthetic",
                "entry_price": ref_price, "stop_loss": 0, "take_profit": 0,
                "position_size_usd": test_size_usd, "generated_at": 0,
            })

    if not signals:
        console.print("[yellow]No signals to quote and no --test-size given.[/yellow]")
        console.print("  Wait for the strategy to fire a BUY, or run:")
        console.print("  python scripts\\jupiter_quote.py --test-size 23")
        return

    console.print(f"Fetching quotes for {len(signals)} signal(s)...\n")

    results = []
    async with httpx.AsyncClient() as client:
        for i, sig in enumerate(signals, 1):
            console.print(f"[dim]({i}/{len(signals)}) {sig['symbol']}...[/dim]", end="")
            r = await quote_signal(client, sig)
            results.append(r)
            tag = "[red]FLAG[/red]" if r.flagged else "[green]OK[/green]"
            console.print(" " + tag)
            await asyncio.sleep(0.3)

    table = Table(title="Jupiter Quotes")
    table.add_column("Symbol")
    table.add_column("USDC In", justify="right")
    table.add_column("Tokens Out", justify="right")
    table.add_column("Fill Price", justify="right")
    table.add_column("Strategy", justify="right")
    table.add_column("Diff %", justify="right")
    table.add_column("Impact %", justify="right")
    table.add_column("Routes", justify="right")
    table.add_column("Status")

    for r in results:
        diff_color = "red" if abs(r.price_diff_pct) > 3 else ("yellow" if abs(r.price_diff_pct) > 1 else "green")
        impact_color = "red" if r.price_impact_pct > 2 else ("yellow" if r.price_impact_pct > 0.5 else "green")
        status = f"[red]{r.flag_reason}[/red]" if r.flagged else "[green]OK[/green]"
        tokens_str = f"{r.tokens_out:,.4f}" if r.tokens_out < 1 else f"{r.tokens_out:,.1f}"
        usdc_str = "$" + f"{r.usdc_in:.2f}"
        table.add_row(
            r.symbol[:10],
            usdc_str,
            tokens_str,
            f"{r.fill_price:.8f}" if r.fill_price else "-",
            f"{r.strategy_price:.8f}" if r.strategy_price else "-",
            f"[{diff_color}]{r.price_diff_pct:+.2f}%[/{diff_color}]" if r.strategy_price else "-",
            f"[{impact_color}]{r.price_impact_pct:.2f}%[/{impact_color}]",
            str(r.route_count) if r.route_count else "-",
            status,
        )
    console.print(table)

    flagged_count = sum(1 for r in results if r.flagged)
    ok_count = len(results) - flagged_count
    console.print(f"\n[green]Quotable: {ok_count}[/green]  [red]Flagged: {flagged_count}[/red]")


@click.command()
@click.option("--test-size", type=float, default=None,
              help="USD size to synthetically quote when no signals exist.")
def main(test_size):
    asyncio.run(main_async(test_size_usd=test_size))


if __name__ == "__main__":
    main()
