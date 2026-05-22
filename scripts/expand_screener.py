from pathlib import Path
import ast

p = Path("scripts/screen_memecoins.py")
src = p.read_text(encoding="utf-8")

# === Step 1: Add new sort variants for each screen ===
# Insert right after the original SCREEN_C_LOTTERY closes (before the dataclass)

addition = """

# Additional sort variants - same thresholds but sorted differently.
# Running all 3 sorts per screen catches tokens that aren't top-by-volume.
# Results are deduped by address in main_async.

SCREEN_A_BY_LIQUIDITY = dict(SCREEN_A_MOMENTUM, sort_by="liquidity")
SCREEN_A_BY_MCAP = dict(SCREEN_A_MOMENTUM, sort_by="marketcap")

SCREEN_B_BY_LIQUIDITY = dict(SCREEN_B_WHALE, sort_by="liquidity")
SCREEN_B_BY_MCAP = dict(SCREEN_B_WHALE, sort_by="marketcap")

SCREEN_C_BY_LIQUIDITY = dict(SCREEN_C_LOTTERY, sort_by="liquidity")
SCREEN_C_BY_24H_CHANGE = dict(SCREEN_C_LOTTERY, sort_by="price_change_24h_percent")

"""

marker = "@dataclass\nclass ScreenedToken:"
if marker in src:
    src = src.replace(marker, addition.lstrip() + "\n\n" + marker)
    print("Added new sort variants (6 new configs)")
else:
    print("WARN: dataclass marker not found - manual review needed")

# === Step 2: Update main_async to run all sorts per screen ===
# Find the screen calls and replace each with a loop over variants

old_a = "        a_tokens, a_err = await fetch_token_list_v3(client, SCREEN_A_MOMENTUM)"
new_a = """        # Run Screen A with 3 sorts (volume, liquidity, mcap) and merge
        a_tokens_all = []
        a_err = None
        for cfg in [SCREEN_A_MOMENTUM, SCREEN_A_BY_LIQUIDITY, SCREEN_A_BY_MCAP]:
            toks, err = await fetch_token_list_v3(client, cfg)
            if err:
                a_err = err
            a_tokens_all.extend(toks or [])
        # Dedupe by address, keeping first occurrence
        seen = set()
        a_tokens = []
        for t in a_tokens_all:
            if t.address not in seen:
                seen.add(t.address)
                a_tokens.append(t)"""

if old_a in src:
    src = src.replace(old_a, new_a)
    print("Patched Screen A to use 3 sorts")
else:
    print("WARN: Screen A call not found in expected form")

old_b = "        b_tokens, b_err = await fetch_token_list_v3(client, SCREEN_B_WHALE)"
new_b = """        # Run Screen B with 3 sorts and merge
        b_tokens_all = []
        b_err = None
        for cfg in [SCREEN_B_WHALE, SCREEN_B_BY_LIQUIDITY, SCREEN_B_BY_MCAP]:
            toks, err = await fetch_token_list_v3(client, cfg)
            if err:
                b_err = err
            b_tokens_all.extend(toks or [])
        seen = set()
        b_tokens = []
        for t in b_tokens_all:
            if t.address not in seen:
                seen.add(t.address)
                b_tokens.append(t)"""

if old_b in src:
    src = src.replace(old_b, new_b)
    print("Patched Screen B to use 3 sorts")
else:
    print("WARN: Screen B call not found in expected form")

old_c = "        c_tokens, c_err = await fetch_token_list_v3(client, SCREEN_C_LOTTERY)"
new_c = """        # Run Screen C with 3 sorts (volume, liquidity, 24h pumpers) and merge
        c_tokens_all = []
        c_err = None
        for cfg in [SCREEN_C_LOTTERY, SCREEN_C_BY_LIQUIDITY, SCREEN_C_BY_24H_CHANGE]:
            toks, err = await fetch_token_list_v3(client, cfg)
            if err:
                c_err = err
            c_tokens_all.extend(toks or [])
        seen = set()
        c_tokens = []
        for t in c_tokens_all:
            if t.address not in seen:
                seen.add(t.address)
                c_tokens.append(t)"""

if old_c in src:
    src = src.replace(old_c, new_c)
    print("Patched Screen C to use 3 sorts")
else:
    print("WARN: Screen C call not found in expected form")

p.write_text(src, encoding="utf-8")

try:
    ast.parse(src.lstrip("\ufeff"))
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
