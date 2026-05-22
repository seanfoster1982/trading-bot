"""
Smoke test. Read-only sanity checks before real trading.

Verifies:
- .env is loaded and required fields are present
- Polymarket Gamma API responds and returns markets
- Polymarket CLOB connects (if private key present)
- Solana RPC responds (if private key present)
- Jupiter API responds

No orders. No money moves. Run this first.
"""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_settings
from exchanges.polymarket import GammaClient, PolymarketExchange
from exchanges.solana import JupiterClient, SolanaExchange, COMMON_MINTS


async def check_polymarket(settings) -> bool:
    print("\n[POLYMARKET]")
    try:
        async with GammaClient(settings.polymarket_gamma_host) as g:
            markets = await g.list_markets(active=True, limit=5)
            print(f"  ✓ Gamma API: {len(markets)} markets fetched")
            if markets:
                m = markets[0]
                print(f"    Sample: {m.question[:80] if m.question else '?'}...")
                print(f"    token_id: {m.token_id}")
    except Exception as e:
        print(f"  ✗ Gamma API failed: {e}")
        return False

    pk = settings.polymarket_private_key.get_secret_value()
    if not pk:
        print("  ⚠ POLYMARKET_PRIVATE_KEY not set — skipping CLOB connect")
        return True

    try:
        poly = PolymarketExchange(
            host=settings.polymarket_host,
            gamma_host=settings.polymarket_gamma_host,
            chain_id=settings.polymarket_chain_id,
            private_key=pk,
            funder_address=settings.polymarket_funder_address,
            signature_type=settings.polymarket_signature_type,
        )
        await poly.connect()
        print("  ✓ CLOB connected (API creds derived)")
        if markets:
            book = await poly.get_order_book(markets[0])
            print(f"  ✓ Order book: best_bid={book.best_bid} best_ask={book.best_ask}")
        await poly.close()
    except Exception as e:
        print(f"  ✗ CLOB connect failed: {e}")
        return False
    return True


async def check_solana(settings) -> bool:
    print("\n[SOLANA]")
    try:
        async with JupiterClient(settings.jupiter_api_host) as j:
            # Quote 1 USDC → SOL
            q = await j.quote(
                input_mint=COMMON_MINTS["USDC"],
                output_mint=COMMON_MINTS["SOL"],
                amount_atomic=1_000_000,
            )
            in_amt = Decimal(q["inAmount"]) / Decimal(10**6)
            out_amt = Decimal(q["outAmount"]) / Decimal(10**9)
            implied_sol_price = in_amt / out_amt if out_amt else Decimal(0)
            print(f"  ✓ Jupiter quote: {in_amt} USDC → {out_amt} SOL")
            print(f"    Implied SOL price: ${implied_sol_price:.2f}")
    except Exception as e:
        print(f"  ✗ Jupiter API failed: {e}")
        return False

    pk = settings.solana_private_key.get_secret_value()
    if not pk:
        print("  ⚠ SOLANA_PRIVATE_KEY not set — skipping RPC connect")
        return True

    try:
        sol = SolanaExchange(
            rpc_url=settings.solana_rpc_url,
            jupiter_host=settings.jupiter_api_host,
            private_key_b58=pk,
        )
        await sol.connect()
        print(f"  ✓ Solana RPC connected, pubkey={sol._public_key[:8]}...")
        await sol.close()
    except Exception as e:
        print(f"  ✗ Solana RPC failed: {e}")
        return False
    return True


async def main() -> int:
    settings = get_settings()
    print(f"Environment: {settings.environment}")
    print(f"Live trading enabled: {settings.live_trading_enabled}")
    print(f"Risk caps: max_pos=${settings.max_position_usd}, "
          f"max_exposure=${settings.max_total_exposure_usd}, "
          f"max_daily_loss=${settings.max_daily_loss_usd}")

    poly_ok = await check_polymarket(settings)
    sol_ok = await check_solana(settings)

    print()
    if poly_ok and sol_ok:
        print("✓ All smoke tests passed")
        return 0
    print("✗ Some smoke tests failed — see above")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
