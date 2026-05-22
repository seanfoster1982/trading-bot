from pathlib import Path
import ast

p = Path("scripts/balance_reader.py")
src = p.read_text(encoding="utf-8")

# ============================================================
# Fix 1: Switch RPC from public to Helius
# ============================================================
old_rpc_const = """# Public mainnet RPC. Rate-limited but fine for read-only balance queries.
# For execution we may switch to a paid/private endpoint.
SOLANA_RPC = 'https://api.mainnet-beta.solana.com'"""

new_rpc_const = """# Helius RPC. Reliable, free tier gives 1M credits/month.
# API key loaded from HELIUS_API_KEY in .env.
import os
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path('.env'))

_HELIUS_KEY = os.getenv('HELIUS_API_KEY')
if not _HELIUS_KEY:
    raise SystemExit('ERROR: HELIUS_API_KEY not set in .env')
SOLANA_RPC = f'https://mainnet.helius-rpc.com/?api-key={_HELIUS_KEY}'"""

if old_rpc_const in src:
    src = src.replace(old_rpc_const, new_rpc_const)
    print("Fix 1: Switched to Helius RPC")
else:
    print("WARN: RPC constant block not found - file may have changed")

# ============================================================
# Fix 2: Token balance parsing - use dict access, not attribute
# ============================================================
old_parse = """    for account in resp.value:
        try:
            info = account.account.data.parsed['info']
            mint = info['mint']
            amount_str = info['tokenAmount']['amount']
            decimals = info['tokenAmount']['decimals']
            raw = int(amount_str)
            if raw == 0:
                continue  # skip empty token accounts
            ui = raw / (10 ** decimals)
            # Look up symbol from our metadata cache
            meta = get_cached_metadata(mint)
            symbol = meta['symbol'] if meta else (
                'USDC' if mint == USDC_MINT else '?'
            )
            balances.append(TokenBalance(
                symbol=symbol, mint=mint,
                raw_amount=raw, decimals=decimals, ui_amount=ui,
            ))
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed accounts"""

new_parse = """    for account in resp.value:
        try:
            # The parsed field is a dict, not a typed object - access with []
            parsed = account.account.data.parsed
            if isinstance(parsed, dict):
                info = parsed.get('info', {})
            else:
                # Some solana-py versions return a typed object instead
                info = parsed.info if hasattr(parsed, 'info') else {}

            mint = info.get('mint') if isinstance(info, dict) else None
            if not mint:
                continue

            ta = info.get('tokenAmount', {}) if isinstance(info, dict) else {}
            amount_str = ta.get('amount', '0')
            decimals = int(ta.get('decimals', 0))
            raw = int(amount_str)
            if raw == 0:
                continue  # skip empty token accounts
            ui = raw / (10 ** decimals) if decimals else 0

            # Look up symbol from our metadata cache
            meta = get_cached_metadata(mint)
            symbol = meta['symbol'] if meta else (
                'USDC' if mint == USDC_MINT else '?'
            )
            balances.append(TokenBalance(
                symbol=symbol, mint=mint,
                raw_amount=raw, decimals=decimals, ui_amount=ui,
            ))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            print(f'  [warn] skipped token account parse: {type(e).__name__}: {e}')
            continue"""

if old_parse in src:
    src = src.replace(old_parse, new_parse)
    print("Fix 2: Token balance parsing made dict-safe with warning on skip")
else:
    print("WARN: parse block not found")

# ============================================================
# Fix 3: Suspicious-data check on SOL balance
# Add a safety check that warns if SOL is 0 (likely RPC issue, not real)
# ============================================================
old_sol = """async def get_sol_balance(client: AsyncClient, owner: Pubkey) -> float:
    \\\"\\\"\\\"Native SOL balance in SOL units.\\\"\\\"\\\"
    resp = await client.get_balance(owner, commitment=Confirmed)
    lamports = resp.value if resp and resp.value is not None else 0
    return lamports / LAMPORTS_PER_SOL"""

new_sol = """async def get_sol_balance(client: AsyncClient, owner: Pubkey) -> float:
    \\\"\\\"\\\"Native SOL balance in SOL units. Raises if RPC response is malformed.\\\"\\\"\\\"
    resp = await client.get_balance(owner, commitment=Confirmed)
    if resp is None:
        raise RuntimeError('RPC returned None for get_balance - aborting')
    if resp.value is None:
        raise RuntimeError('RPC returned value=None for get_balance - aborting')
    lamports = resp.value
    return lamports / LAMPORTS_PER_SOL"""

if old_sol in src:
    src = src.replace(old_sol, new_sol)
    print("Fix 3: Added explicit error on malformed SOL balance response")

# Mask the API key in any printed output - never log it
old_print = """    console.print(f'Querying Solana RPC: {SOLANA_RPC}')"""
new_print = """    # Mask the API key when printing the RPC URL
    rpc_safe = SOLANA_RPC.split('api-key=')[0] + 'api-key=***'
    console.print(f'Querying Solana RPC: {rpc_safe}')"""
if old_print in src:
    src = src.replace(old_print, new_print)
    print("Fix 4: API key masked in console output")

p.write_text(src, encoding="utf-8")

try:
    ast.parse(src)
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
