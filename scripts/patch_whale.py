from pathlib import Path
p = Path(r'scripts\whale_risk.py')
src = p.read_text(encoding='utf-8')

# Remove exit-liquidity function
old_exit = '''async def fetch_exit_liquidity(client: httpx.AsyncClient, address: str) -> dict | None:
    headers = {
        \"X-API-KEY\": BIRDEYE_KEY,
        \"x-chain\": \"solana\",
        \"accept\": \"application/json\",
    }
    try:
        r = await client.get(
            f\"{BIRDEYE_BASE}/defi/v3/token/exit-liquidity\",
            headers=headers,
            params={\"address\": address},
            timeout=15.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get(\"success\"):
            return None
        return data.get(\"data\")
    except Exception:
        return None'''
new_exit = '# Exit-liquidity endpoint is EVM-only on Birdeye. Using liquidity/mcap proxy instead.'
if old_exit in src:
    src = src.replace(old_exit, new_exit)
    print('Removed exit-liquidity fetch')

# Replace check_whale_risk with correct field names and direct top_10_pct
old_check = '''async def check_whale_risk(
    client: httpx.AsyncClient, symbol: str, address: str
) -> WhaleReport | None:
    \"\"\"Full whale risk analysis for one token.\"\"\"
    # Pull all data in parallel
    holders_task = fetch_token_holders(client, address)
    stats_task = fetch_holder_stats(client, address)
    exit_task = fetch_exit_liquidity(client, address)
    overview_task = fetch_token_overview(client, address)
    holders_result, stats, exit_data, overview = await asyncio.gather(
        holders_task, stats_task, exit_task, overview_task
    )

    holders, holders_err = holders_result
    if holders_err and not stats and not overview:
        return None

    # Top 10 concentration
    total_supply = 0
    market_cap = 0
    if overview:
        total_supply = to_float(overview.get(\"supply\"))
        market_cap = to_float(overview.get(\"mc\"))

    top_10_held = sum(to_float(h.get(\"ui_amount\")) for h in holders[:10])
    top_10_pct = (top_10_held / total_supply * 100) if total_supply > 0 else 0.0

    # Holder count
    holder_count = 0
    if stats:
        holder_count = to_int(stats.get(\"holder\")) or to_int(stats.get(\"total\"))
    if not holder_count and overview:
        holder_count = to_int(overview.get(\"holder\"))

    # Compare to previous check (~24h ago)
    prev = get_previous_report(address, hours_ago=24)
    if prev:
        top_10_pct_delta = top_10_pct - prev[\"top_10_pct\"]
        holder_count_delta = holder_count - prev[\"holder_count\"]
    else:
        top_10_pct_delta = 0.0
        holder_count_delta = 0

    # Exit liquidity
    exit_liq_usd = 0.0
    if exit_data:
        exit_liq_usd = to_float(exit_data.get(\"exitLiquidity\")) or to_float(exit_data.get(\"value\"))

    score, flags = compute_whale_risk_score(
        top_10_pct=top_10_pct,
        top_10_pct_delta=top_10_pct_delta,
        holder_count=holder_count,
        holder_count_delta=holder_count_delta,
        exit_liquidity_usd=exit_liq_usd,
        market_cap_usd=market_cap,
    )

    return WhaleReport(
        address=address,
        symbol=symbol,
        whale_risk_score=score,
        top_10_pct=top_10_pct,
        top_10_pct_delta_24h=top_10_pct_delta,
        holder_count=holder_count,
        holder_count_delta_24h=holder_count_delta,
        exit_liquidity_usd=exit_liq_usd,
        flags=flags,
    )'''
new_check = '''async def check_whale_risk(
    client: httpx.AsyncClient, symbol: str, address: str
) -> WhaleReport | None:
    \"\"\"Full whale risk analysis for one token.\"\"\"
    stats_task = fetch_holder_stats(client, address)
    overview_task = fetch_token_overview(client, address)
    stats, overview = await asyncio.gather(stats_task, overview_task)

    if not stats and not overview:
        return None

    top_10_pct = 0.0
    holder_count = 0
    if stats:
        top_10_pct = to_float(stats.get(\"top_10_pct\"))
        holder_count = to_int(stats.get(\"holder\"))

    market_cap = 0.0
    liquidity = 0.0
    if overview:
        market_cap = to_float(overview.get(\"marketCap\"))
        liquidity = to_float(overview.get(\"liquidity\"))
        if not holder_count:
            holder_count = to_int(overview.get(\"holder\"))

    prev = get_previous_report(address, hours_ago=24)
    if prev:
        top_10_pct_delta = top_10_pct - prev[\"top_10_pct\"]
        holder_count_delta = holder_count - prev[\"holder_count\"]
    else:
        top_10_pct_delta = 0.0
        holder_count_delta = 0

    score, flags = compute_whale_risk_score(
        top_10_pct=top_10_pct,
        top_10_pct_delta=top_10_pct_delta,
        holder_count=holder_count,
        holder_count_delta=holder_count_delta,
        exit_liquidity_usd=liquidity,
        market_cap_usd=market_cap,
    )

    return WhaleReport(
        address=address,
        symbol=symbol,
        whale_risk_score=score,
        top_10_pct=top_10_pct,
        top_10_pct_delta_24h=top_10_pct_delta,
        holder_count=holder_count,
        holder_count_delta_24h=holder_count_delta,
        exit_liquidity_usd=liquidity,
        flags=flags,
    )'''
if old_check in src:
    src = src.replace(old_check, new_check)
    print('Replaced check_whale_risk with corrected field names')

p.write_text(src, encoding='utf-8')
print('PATCH APPLIED')
