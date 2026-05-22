from pathlib import Path
import ast

p = Path("scripts/jupiter_quote.py")
src = p.read_text(encoding="utf-8")

# Replace the hardcoded get_token_decimals function with a real lookup
old_func = """def get_token_decimals(address):
    # Most Solana memecoins use 6 decimals; would query Birdeye for exact if needed
    return 6"""

new_func = """def get_token_decimals(address):
    # Real lookup via token_metadata module - hits cache first, Birdeye on miss
    from token_metadata import get_decimals
    d = get_decimals(address)
    if d is None:
        # Fallback only if metadata lookup completely failed - log it loudly
        print(f"WARN: no decimals found for {address}, falling back to 6")
        return 6
    return d"""

if old_func in src:
    src = src.replace(old_func, new_func)
    print("Patched get_token_decimals to use real lookup")
else:
    print("WARN: old function block not found")

p.write_text(src, encoding="utf-8")

try:
    ast.parse(src)
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
