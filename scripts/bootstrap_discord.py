"""Discord channel bootstrap — idempotent channel creation for Trading Bot OPS.

Creates category 'TRADING BOT OPS' and required channels if they don't exist.
Reuses existing matching channels; never deletes. Output CREATED/EXISTING/FAILED per channel.

Usage:
    python scripts/bootstrap_discord.py --check      # env presence check only
    python scripts/bootstrap_discord.py --bootstrap  # create category + channels
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
load_dotenv(ROOT / ".env")

CATEGORY_NAME = "TRADING BOT OPS"
CHANNELS = [
    "operations",
    "trade-executions",
    "signals",
    "risk-vetoes",
    "security",
    "airdrops",
    "market-intelligence",
    "cursor-dev",
    "system-health",
    "audit-log",
]

ENV_NAMES = [
    "DISCORD_ENABLED",
    "DISCORD_BOT_TOKEN",
    "DISCORD_APPLICATION_ID",
    "DISCORD_GUILD_ID",
    "DISCORD_OPERATOR_ID",
    "DISCORD_WEBHOOK_URL",
]


def _present(name: str) -> bool:
    return bool((os.getenv(name) or "").strip())


def check() -> int:
    """Check env presence without connecting."""
    print("DISCORD BOOTSTRAP STATUS")
    print(f"Category: {CATEGORY_NAME}")
    print("Planned channels:")
    for c in CHANNELS:
        print(f"  #{c}")
    print("Env presence (values hidden):")
    for n in ENV_NAMES:
        if n == "DISCORD_ENABLED":
            raw = (os.getenv(n) or "").strip().lower()
            print(f"  {n}: set={bool(raw)} enabled={raw in ('1','true','yes','on')}")
        else:
            print(f"  {n}: {'YES' if _present(n) else 'NO'}")
    
    token_present = _present("DISCORD_BOT_TOKEN")
    app_present = _present("DISCORD_APPLICATION_ID")
    guild_present = _present("DISCORD_GUILD_ID")
    operator_present = _present("DISCORD_OPERATOR_ID")
    
    if token_present and app_present and guild_present and operator_present:
        print("DISCORD: READY_FOR_BOOTSTRAP")
        return 0
    elif token_present:
        print("DISCORD: READY_FOR_CREDS (partial config)")
        return 0
    else:
        print("DISCORD: WAITING (missing credentials)")
        return 1


async def bootstrap() -> int:
    """Create category and channels idempotently."""
    try:
        import discord
    except ImportError:
        print("INSTALL_NEEDED: pip install discord.py")
        return 2
    
    token = (os.getenv("DISCORD_BOT_TOKEN") or "").strip()
    guild_id_str = (os.getenv("DISCORD_GUILD_ID") or "").strip()
    
    if not token:
        print("FAILED: DISCORD_BOT_TOKEN missing")
        return 1
    if not guild_id_str or not guild_id_str.isdigit():
        print("FAILED: DISCORD_GUILD_ID missing or invalid")
        return 1
    
    guild_id = int(guild_id_str)
    
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    results: dict[str, str] = {}
    exit_code = 0
    
    @client.event
    async def on_ready():
        nonlocal exit_code, results
        
        guild = client.get_guild(guild_id)
        if not guild:
            print(f"FAILED: Cannot access guild {guild_id}")
            exit_code = 1
            await client.close()
            return
        
        print(f"Connected as {client.user} to guild '{guild.name}'")
        
        existing_categories = {c.name.upper(): c for c in guild.categories}
        category = existing_categories.get(CATEGORY_NAME.upper())
        
        if category:
            print(f"EXISTING: Category '{CATEGORY_NAME}'")
        else:
            try:
                category = await guild.create_category(CATEGORY_NAME)
                print(f"CREATED: Category '{CATEGORY_NAME}'")
            except discord.Forbidden:
                print(f"FAILED: Category '{CATEGORY_NAME}' (missing Manage Channels permission)")
                exit_code = 1
                await client.close()
                return
            except discord.HTTPException as e:
                print(f"FAILED: Category '{CATEGORY_NAME}' ({e})")
                exit_code = 1
                await client.close()
                return
        
        existing_channels = {}
        for ch in guild.text_channels:
            if ch.category_id == category.id:
                existing_channels[ch.name.lower()] = ch
        
        for channel_name in CHANNELS:
            key = channel_name.lower()
            if key in existing_channels:
                results[channel_name] = "EXISTING"
                print(f"EXISTING: #{channel_name}")
            else:
                try:
                    await guild.create_text_channel(channel_name, category=category)
                    results[channel_name] = "CREATED"
                    print(f"CREATED: #{channel_name}")
                except discord.Forbidden:
                    results[channel_name] = "FAILED:PERMISSION"
                    print(f"FAILED: #{channel_name} (missing permission)")
                    exit_code = 1
                except discord.HTTPException as e:
                    results[channel_name] = f"FAILED:{e}"
                    print(f"FAILED: #{channel_name} ({e})")
                    exit_code = 1
        
        await client.close()
    
    try:
        await asyncio.wait_for(client.start(token), timeout=60.0)
    except asyncio.TimeoutError:
        print("FAILED: Connection timeout")
        return 1
    except discord.LoginFailure:
        print("FAILED: Invalid bot token")
        return 1
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return 1
    
    created = sum(1 for v in results.values() if v == "CREATED")
    existing = sum(1 for v in results.values() if v == "EXISTING")
    failed = sum(1 for v in results.values() if v.startswith("FAILED"))
    
    print(f"\nSummary: {created} created, {existing} existing, {failed} failed")
    return exit_code


def main() -> None:
    p = argparse.ArgumentParser(description="Discord channel bootstrap for Trading Bot OPS")
    p.add_argument("--check", action="store_true", help="Check env presence only (no connection)")
    p.add_argument("--bootstrap", action="store_true", help="Create category and channels")
    args = p.parse_args()
    
    if args.bootstrap:
        raise SystemExit(asyncio.run(bootstrap()))
    else:
        raise SystemExit(check())


if __name__ == "__main__":
    main()
