"""Discord gateway — persistent bot process for Trading Bot OPS console.

This is the main Discord operations console. Reuses stop_control for HALT/RESUME.
No EVM/Solana private keys, no transaction signing, no /buy /sell /trade commands.

Usage:
    python scripts/discord_gateway.py --check         # env + dependency check
    python scripts/discord_gateway.py --connect-test  # connect and verify identity
    python scripts/discord_gateway.py                 # run persistent gateway

Slash commands:
    Read-only: /status /positions /pnl /risk /signals /sources /scan /help
    Control (operator only): /halt /stop /pause /emergency_stop /resume
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
load_dotenv(ROOT / ".env")

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "discord_gateway.log"
STATE_DIR = ROOT / "data" / "discord"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_DB = STATE_DIR / "state.sqlite3"


def _setup_logging() -> logging.Logger:
    """Configure logging to file with token redaction."""
    logger = logging.getLogger("discord_gateway")
    logger.setLevel(logging.DEBUG)
    
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    
    class TokenRedactingFormatter(logging.Formatter):
        def format(self, record):
            msg = super().format(record)
            token = (os.getenv("DISCORD_BOT_TOKEN") or "")[:20]
            if token and len(token) > 10:
                msg = msg.replace(token, "[REDACTED]")
            return re.sub(r"(Bot |Bearer )?[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{27,}", "[REDACTED_TOKEN]", msg)
    
    formatter = TokenRedactingFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(TokenRedactingFormatter("[%(levelname)s] %(message)s"))
    logger.addHandler(console)
    
    return logger


log = _setup_logging()


def _enabled() -> bool:
    return (os.getenv("DISCORD_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")


def _get_config() -> dict:
    return {
        "token": (os.getenv("DISCORD_BOT_TOKEN") or "").strip(),
        "app_id": (os.getenv("DISCORD_APPLICATION_ID") or "").strip(),
        "guild_id": (os.getenv("DISCORD_GUILD_ID") or "").strip(),
        "operator_id": (os.getenv("DISCORD_OPERATOR_ID") or "").strip(),
    }


def check() -> int:
    """Verify env presence, IDs parse, dependencies, stop_control import, sqlite access. No trading/RPC."""
    print("DISCORD GATEWAY CHECK")
    print("=" * 50)
    
    errors = []
    
    cfg = _get_config()
    print(f"DISCORD_ENABLED: {_enabled()}")
    print(f"DISCORD_BOT_TOKEN: {'YES' if cfg['token'] else 'NO'}")
    print(f"DISCORD_APPLICATION_ID: {'YES' if cfg['app_id'] else 'NO'}", end="")
    if cfg['app_id']:
        if cfg['app_id'].isdigit():
            print(" (valid)")
        else:
            print(" (INVALID - not numeric)")
            errors.append("APPLICATION_ID must be numeric")
    else:
        print()
    
    print(f"DISCORD_GUILD_ID: {'YES' if cfg['guild_id'] else 'NO'}", end="")
    if cfg['guild_id']:
        if cfg['guild_id'].isdigit():
            print(" (valid)")
        else:
            print(" (INVALID - not numeric)")
            errors.append("GUILD_ID must be numeric")
    else:
        print()
    
    print(f"DISCORD_OPERATOR_ID: {'YES' if cfg['operator_id'] else 'NO'}", end="")
    if cfg['operator_id']:
        if cfg['operator_id'].isdigit():
            print(" (valid)")
        else:
            print(" (INVALID - not numeric)")
            errors.append("OPERATOR_ID must be numeric")
    else:
        print()
    
    print()
    print("Dependency check:")
    try:
        import discord
        print(f"  discord.py: {discord.__version__}")
    except ImportError:
        print("  discord.py: MISSING (pip install discord.py)")
        errors.append("discord.py not installed")
    
    print()
    print("stop_control import:")
    try:
        import stop_control
        stop_control.ensure_initialized()
        snap = stop_control.snapshot()
        print(f"  import: OK")
        print(f"  mode: {snap.get('mode')}")
        print(f"  generation: {snap.get('generation')}")
    except Exception as e:
        print(f"  import: FAILED ({e})")
        errors.append(f"stop_control failed: {e}")
    
    print()
    print("SQLite access (trading DB):")
    try:
        trading_db = ROOT / "data" / "memecoins.db"
        if trading_db.exists():
            conn = sqlite3.connect(trading_db, timeout=10)
            conn.execute("SELECT 1")
            conn.close()
            print(f"  {trading_db}: OK")
        else:
            print(f"  {trading_db}: NOT EXISTS (will be created on first trade)")
    except Exception as e:
        print(f"  {trading_db}: FAILED ({e})")
        errors.append(f"Trading DB access failed: {e}")
    
    print()
    print("Discord state DB:")
    try:
        _init_state_db()
        print(f"  {STATE_DB}: OK")
    except Exception as e:
        print(f"  {STATE_DB}: FAILED ({e})")
        errors.append(f"State DB failed: {e}")
    
    print()
    if errors:
        print(f"RESULT: FAILED ({len(errors)} errors)")
        for e in errors:
            print(f"  - {e}")
        return 1
    else:
        print("RESULT: PASS")
        return 0


async def connect_test() -> int:
    """Connect to Discord, confirm bot identity + guild access. No channel changes."""
    try:
        import discord
    except ImportError:
        print("FAILED: discord.py not installed")
        return 1
    
    cfg = _get_config()
    if not cfg["token"]:
        print("FAILED: DISCORD_BOT_TOKEN missing")
        return 1
    if not cfg["guild_id"] or not cfg["guild_id"].isdigit():
        print("FAILED: DISCORD_GUILD_ID missing or invalid")
        return 1
    
    guild_id = int(cfg["guild_id"])
    
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    result = {"success": False}
    
    @client.event
    async def on_ready():
        guild = client.get_guild(guild_id)
        if guild:
            print(f"CONNECTED: Bot={client.user.name} ({client.user.id})")
            print(f"GUILD: {guild.name} ({guild.id})")
            print(f"MEMBERS: {guild.member_count}")
            result["success"] = True
        else:
            print(f"BOT OK but cannot access guild {guild_id}")
            result["success"] = False
        await client.close()
    
    try:
        await asyncio.wait_for(client.start(cfg["token"]), timeout=30.0)
    except asyncio.TimeoutError:
        print("FAILED: Connection timeout")
        return 1
    except discord.LoginFailure:
        print("FAILED: Invalid bot token")
        return 1
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return 1
    
    return 0 if result["success"] else 1


def _init_state_db():
    """Initialize Discord state SQLite for publisher checkpoints."""
    conn = sqlite3.connect(STATE_DB, timeout=30)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS publisher_checkpoint (
            table_name TEXT PRIMARY KEY,
            last_id INTEGER NOT NULL,
            last_ts REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posted_events (
            event_id TEXT PRIMARY KEY,
            posted_at REAL NOT NULL,
            channel TEXT
        )
    """)
    conn.commit()
    conn.close()


def _get_checkpoint(table_name: str) -> tuple[int, float]:
    """Get last processed ID and timestamp for a table."""
    conn = sqlite3.connect(STATE_DB, timeout=10)
    row = conn.execute(
        "SELECT last_id, last_ts FROM publisher_checkpoint WHERE table_name=?",
        (table_name,)
    ).fetchone()
    conn.close()
    return (row[0], row[1]) if row else (0, 0.0)


def _set_checkpoint(table_name: str, last_id: int, last_ts: float):
    """Update checkpoint for a table."""
    conn = sqlite3.connect(STATE_DB, timeout=10)
    conn.execute(
        "INSERT OR REPLACE INTO publisher_checkpoint VALUES (?,?,?)",
        (table_name, last_id, last_ts)
    )
    conn.commit()
    conn.close()


class DiscordGateway:
    """Persistent Discord bot with slash commands and event publisher."""
    
    CHANNEL_ROUTES = {
        "executions": "trade-executions",
        "BUY": "signals",
        "SELL": "signals",
        "HOLD": "signals",
        "NO_TRADE": "signals",
        "BLOCKED": "risk-vetoes",
        "goplus": "security",
        "HALT": "operations",
        "RESUME": "operations",
        "health": "system-health",
        "airdrop": "airdrops",
    }
    
    def __init__(self):
        import discord
        from discord import app_commands
        
        self.cfg = _get_config()
        self.guild_id = int(self.cfg["guild_id"]) if self.cfg["guild_id"].isdigit() else 0
        self.operator_id = int(self.cfg["operator_id"]) if self.cfg["operator_id"].isdigit() else 0
        
        intents = discord.Intents.default()
        intents.message_content = True
        
        self.client = discord.Client(intents=intents)
        self.tree = app_commands.CommandTree(self.client)
        self.guild_obj: Optional[discord.Guild] = None
        self.channels: dict[str, discord.TextChannel] = {}
        self._publisher_task: Optional[asyncio.Task] = None
        self._running = True
        
        self._register_commands()
        
        @self.client.event
        async def on_ready():
            await self._on_ready()
    
    def _is_authorized(self, interaction) -> bool:
        """Check if interaction is from authorized operator in correct guild."""
        if interaction.guild_id != self.guild_id:
            return False
        if interaction.user.id != self.operator_id:
            return False
        return True
    
    async def _unauthorized_response(self, interaction):
        """Send ephemeral NOT AUTHORIZED response."""
        import discord
        await interaction.response.send_message(
            "NOT AUTHORIZED: This command requires operator privileges.",
            ephemeral=True
        )
    
    def _register_commands(self):
        """Register all slash commands with guild scope."""
        import discord
        from discord import app_commands
        
        guild = discord.Object(id=self.guild_id)
        
        @self.tree.command(name="help", description="Show available commands", guild=guild)
        async def cmd_help(interaction: discord.Interaction):
            embed = discord.Embed(
                title="Trading Bot Discord Console",
                description="Read-only monitoring and operator control. **Discord cannot buy/sell.**",
                color=discord.Color.blue()
            )
            embed.add_field(
                name="Read-only Commands",
                value=(
                    "`/status` - System status, mode, open positions\n"
                    "`/positions` - Current open positions\n"
                    "`/pnl` - Realized P&L summary\n"
                    "`/risk` - Risk limits and budget\n"
                    "`/signals` - Recent decision signals\n"
                    "`/sources` - Integration status\n"
                    "`/scan` - Fresh listing discovery (display only)"
                ),
                inline=False
            )
            embed.add_field(
                name="Control Commands (Operator Only)",
                value=(
                    "`/halt` `/stop` `/pause` `/emergency_stop` - Halt new buys\n"
                    "`/resume` - Resume new buys\n\n"
                    "**HALT = no new buys only**\n"
                    "Existing positions still managed. In-flight txs cannot be undone."
                ),
                inline=False
            )
            embed.set_footer(text="Telegram has parallel control. Both use same stop_control.")
            await interaction.response.send_message(embed=embed)
        
        @self.tree.command(name="status", description="System status overview", guild=guild)
        async def cmd_status(interaction: discord.Interaction):
            await self._cmd_status(interaction)
        
        @self.tree.command(name="positions", description="Current open positions", guild=guild)
        async def cmd_positions(interaction: discord.Interaction):
            await self._cmd_positions(interaction)
        
        @self.tree.command(name="pnl", description="Realized P&L summary", guild=guild)
        async def cmd_pnl(interaction: discord.Interaction):
            await self._cmd_pnl(interaction)
        
        @self.tree.command(name="risk", description="Risk limits and budget status", guild=guild)
        async def cmd_risk(interaction: discord.Interaction):
            await self._cmd_risk(interaction)
        
        @self.tree.command(name="signals", description="Recent decision signals", guild=guild)
        async def cmd_signals(interaction: discord.Interaction):
            await self._cmd_signals(interaction)
        
        @self.tree.command(name="sources", description="Integration/data source status", guild=guild)
        async def cmd_sources(interaction: discord.Interaction):
            await self._cmd_sources(interaction)
        
        @self.tree.command(name="scan", description="Fresh listing discovery (display only)", guild=guild)
        async def cmd_scan(interaction: discord.Interaction):
            await self._cmd_scan(interaction)
        
        for alias in ["halt", "stop", "pause", "emergency_stop"]:
            @self.tree.command(name=alias, description="HALT new buys (existing positions still managed)", guild=guild)
            async def cmd_halt(interaction: discord.Interaction, _alias=alias):
                await self._cmd_halt(interaction)
        
        @self.tree.command(name="resume", description="Resume new buys", guild=guild)
        async def cmd_resume(interaction: discord.Interaction):
            await self._cmd_resume(interaction)
    
    async def _on_ready(self):
        """Called when bot is ready."""
        import discord
        
        log.info(f"Connected as {self.client.user} (ID: {self.client.user.id})")
        
        self.guild_obj = self.client.get_guild(self.guild_id)
        if not self.guild_obj:
            log.error(f"Cannot access guild {self.guild_id}")
            return
        
        log.info(f"Guild: {self.guild_obj.name}")
        
        for ch in self.guild_obj.text_channels:
            name = ch.name.lower()
            if name in [c.lower() for c in self.CHANNEL_ROUTES.values()]:
                self.channels[name] = ch
        
        log.info(f"Found channels: {list(self.channels.keys())}")
        
        guild_obj = discord.Object(id=self.guild_id)
        self.tree.copy_global_to(guild=guild_obj)
        await self.tree.sync(guild=guild_obj)
        log.info("Slash commands synced to guild")
        
        _init_state_db()
        self._publisher_task = asyncio.create_task(self._publisher_loop())
        
        if "operations" in self.channels:
            await self.channels["operations"].send(
                f"**Discord Gateway Online** — {datetime.utcnow().isoformat(timespec='seconds')}Z"
            )
    
    async def _cmd_status(self, interaction):
        """Show system status."""
        import discord
        import stop_control
        from whale_config import (
            RH_LIVE_ENABLED, RH_TRADE_USD, RH_BUDGET_USD, RH_MAX_OPEN
        )
        
        try:
            stop_control.ensure_initialized()
            snap = stop_control.snapshot()
            mode = snap.get("mode", "UNKNOWN")
            gen = snap.get("generation", 0)
        except Exception as e:
            mode = f"ERROR: {e}"
            gen = 0
        
        try:
            import goplus_security as gp
            gp_cfg = gp.read_config()
            gp_status = f"{'enabled' if gp_cfg.enabled else 'disabled'} ({'configured' if gp_cfg.configured else 'not configured'})"
        except Exception:
            gp_status = "unknown"
        
        trading_db = ROOT / "data" / "memecoins.db"
        open_count = 0
        budget_left = RH_BUDGET_USD
        if trading_db.exists():
            try:
                conn = sqlite3.connect(trading_db, timeout=10)
                open_count = conn.execute(
                    "SELECT COUNT(*) FROM rh_live_trades WHERE closed_at IS NULL"
                ).fetchone()[0]
                spent = conn.execute(
                    "SELECT COALESCE(SUM(usd_spent), 0) FROM rh_live_trades"
                ).fetchone()[0]
                budget_left = RH_BUDGET_USD - float(spent)
                conn.close()
            except Exception:
                pass
        
        embed = discord.Embed(
            title="Trading Bot Status",
            color=discord.Color.green() if mode == "ACTIVE" else discord.Color.red()
        )
        embed.add_field(name="RH Live", value="ENABLED" if RH_LIVE_ENABLED else "DISABLED", inline=True)
        embed.add_field(name="Solana", value="DISABLED", inline=True)
        embed.add_field(name="Stop Mode", value=f"{mode} (gen={gen})", inline=True)
        embed.add_field(name="Open Positions", value=f"{open_count}/{RH_MAX_OPEN}", inline=True)
        embed.add_field(name="Budget Left", value=f"${budget_left:.2f}", inline=True)
        embed.add_field(name="Trade Size", value=f"${RH_TRADE_USD:.0f}", inline=True)
        embed.add_field(name="GoPlus", value=gp_status, inline=True)
        embed.add_field(name="Telegram", value="parallel control", inline=True)
        embed.set_footer(text=f"UTC: {datetime.utcnow().isoformat(timespec='seconds')}")
        
        await interaction.response.send_message(embed=embed)
    
    async def _cmd_positions(self, interaction):
        """Show open positions."""
        import discord
        
        trading_db = ROOT / "data" / "memecoins.db"
        if not trading_db.exists():
            await interaction.response.send_message("No trading database yet.")
            return
        
        try:
            conn = sqlite3.connect(trading_db, timeout=10)
            rows = conn.execute("""
                SELECT symbol, address, source, usd_spent, opened_at, peak_pnl_pct
                FROM rh_live_trades WHERE closed_at IS NULL
                ORDER BY opened_at DESC LIMIT 15
            """).fetchall()
            conn.close()
        except Exception as e:
            await interaction.response.send_message(f"DB error: {e}")
            return
        
        if not rows:
            await interaction.response.send_message("No open positions.")
            return
        
        embed = discord.Embed(title="Open Positions", color=discord.Color.blue())
        for sym, addr, src, usd, opened, peak in rows:
            opened_str = datetime.fromtimestamp(opened).strftime("%m/%d %H:%M") if opened else "?"
            embed.add_field(
                name=f"{sym or '?'} ({src})",
                value=f"${usd:.2f} | Peak: {peak:.1f}% | {opened_str}\n`{addr[:10]}...`",
                inline=True
            )
        
        await interaction.response.send_message(embed=embed)
    
    async def _cmd_pnl(self, interaction):
        """Show P&L summary."""
        import discord
        
        trading_db = ROOT / "data" / "memecoins.db"
        if not trading_db.exists():
            await interaction.response.send_message("No trading database yet.")
            return
        
        try:
            conn = sqlite3.connect(trading_db, timeout=10)
            closed = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(pnl_usd), 0) FROM rh_live_trades WHERE closed_at IS NOT NULL"
            ).fetchone()
            open_count = conn.execute(
                "SELECT COUNT(*) FROM rh_live_trades WHERE closed_at IS NULL"
            ).fetchone()[0]
            recent = conn.execute("""
                SELECT symbol, close_reason, pnl_usd FROM rh_live_trades
                WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 5
            """).fetchall()
            conn.close()
        except Exception as e:
            await interaction.response.send_message(f"DB error: {e}")
            return
        
        total_closed, total_pnl = closed
        color = discord.Color.green() if total_pnl >= 0 else discord.Color.red()
        
        embed = discord.Embed(title="P&L Summary", color=color)
        embed.add_field(name="Realized P&L", value=f"${total_pnl:+.2f}", inline=True)
        embed.add_field(name="Closed Trades", value=str(total_closed), inline=True)
        embed.add_field(name="Open Trades", value=str(open_count), inline=True)
        
        if recent:
            recent_str = "\n".join([
                f"{sym}: ${pnl:+.2f} ({reason})"
                for sym, reason, pnl in recent
            ])
            embed.add_field(name="Recent Closes", value=recent_str, inline=False)
        
        await interaction.response.send_message(embed=embed)
    
    async def _cmd_risk(self, interaction):
        """Show risk limits."""
        import discord
        from whale_config import (
            RH_TRADE_USD, RH_BUDGET_USD, RH_MAX_OPEN, RH_MAX_REALIZED_LOSS_USD,
            RH_STOP_PCT, RH_MAX_HOLD_MINUTES, RH_SLIPPAGE_BPS
        )
        
        embed = discord.Embed(title="Risk Configuration", color=discord.Color.orange())
        embed.add_field(name="Trade Size", value=f"${RH_TRADE_USD:.0f}", inline=True)
        embed.add_field(name="Total Budget", value=f"${RH_BUDGET_USD:.0f}", inline=True)
        embed.add_field(name="Max Open", value=str(RH_MAX_OPEN), inline=True)
        embed.add_field(name="Max Loss (halt)", value=f"${RH_MAX_REALIZED_LOSS_USD:.0f}", inline=True)
        embed.add_field(name="Stop Loss", value=f"{RH_STOP_PCT:.0f}%", inline=True)
        embed.add_field(name="Max Hold", value=f"{RH_MAX_HOLD_MINUTES}min", inline=True)
        embed.add_field(name="Slippage", value=f"{RH_SLIPPAGE_BPS}bps", inline=True)
        embed.set_footer(text="These are HARD CAPS. Cannot be changed via Discord.")
        
        await interaction.response.send_message(embed=embed)
    
    async def _cmd_signals(self, interaction):
        """Show recent decision signals."""
        import discord
        
        trading_db = ROOT / "data" / "memecoins.db"
        if not trading_db.exists():
            await interaction.response.send_message("No trading database yet.")
            return
        
        try:
            conn = sqlite3.connect(trading_db, timeout=10)
            rows = conn.execute("""
                SELECT ts, decision, symbol, asset, risk_code, security_status
                FROM rh_decisions
                ORDER BY id DESC LIMIT 10
            """).fetchall()
            conn.close()
        except Exception as e:
            await interaction.response.send_message(f"DB error: {e}")
            return
        
        if not rows:
            await interaction.response.send_message("No recent signals.")
            return
        
        embed = discord.Embed(title="Recent Signals", color=discord.Color.purple())
        for ts, decision, sym, asset, risk, sec in rows:
            ts_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "?"
            addr_short = (asset or "")[:8] + "..." if asset else ""
            embed.add_field(
                name=f"{ts_str} {decision or '?'}",
                value=f"{sym or addr_short} | risk={risk or '?'} sec={sec or '?'}",
                inline=False
            )
        
        await interaction.response.send_message(embed=embed)
    
    async def _cmd_sources(self, interaction):
        """Show integration/source status."""
        import discord
        
        sources = []
        
        def _yn(val): return "YES" if val else "NO"
        def _present(name): return bool((os.getenv(name) or "").strip())
        
        sources.append(("DexScreener", "Free", "n/a", "existing"))
        sources.append(("Birdeye", "Starter", _yn(_present("BIRDEYE_API_KEY")), "existing"))
        
        gp_cfg = _yn(_present("GOPLUS_APP_KEY") and _present("GOPLUS_APP_SECRET"))
        gp_en = (os.getenv("GOPLUS_ENABLED") or "").lower() in ("1", "true", "yes", "on")
        sources.append(("GoPlus", "Free", gp_cfg, _yn(gp_en)))
        
        sources.append(("Etherscan", "Free", _yn(_present("ETHERSCAN_API_KEY")), "NO"))
        
        x_cfg = _yn(_present("X_BEARER_TOKEN"))
        x_en = (os.getenv("X_ENABLED") or "").lower() in ("1", "true", "yes", "on")
        sources.append(("X/Twitter", "Pay/use", x_cfg, _yn(x_en)))
        
        d_cfg = _yn(_present("DISCORD_BOT_TOKEN"))
        d_en = _enabled()
        sources.append(("Discord", "Free", d_cfg, _yn(d_en)))
        
        tg_cfg = _yn(_present("TELEGRAM_BOT_TOKEN") and _present("TELEGRAM_CHAT_ID"))
        sources.append(("Telegram", "Free", tg_cfg, "YES"))
        
        embed = discord.Embed(title="Integration Status", color=discord.Color.teal())
        for name, plan, cfg, live in sources:
            embed.add_field(
                name=name,
                value=f"Plan: {plan}\nConfigured: {cfg}\nLive: {live}",
                inline=True
            )
        
        await interaction.response.send_message(embed=embed)
    
    async def _cmd_scan(self, interaction):
        """READ-ONLY discovery display. MUST NOT call cycle() or execution path."""
        import discord
        import httpx
        from whale_config import RH_MIN_LIQUIDITY, RH_MIN_VOLUME_1H
        
        await interaction.response.defer()
        
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://api.dexscreener.com/token-pairs/v1/robinhood/0x4300000000000000000000000000000000000004",
                    timeout=15.0
                )
                pairs = r.json() if r.status_code == 200 else []
        except Exception as e:
            await interaction.followup.send(f"DexScreener error: {e}")
            return
        
        candidates = []
        if isinstance(pairs, list):
            for p in pairs[:50]:
                base = p.get("baseToken") or {}
                addr = base.get("address")
                if not addr:
                    continue
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                vol = float((p.get("volume") or {}).get("h1") or 0)
                if liq < RH_MIN_LIQUIDITY or vol < RH_MIN_VOLUME_1H:
                    continue
                created_ms = p.get("pairCreatedAt") or 0
                age_min = (time.time() - created_ms / 1000) / 60 if created_ms else None
                candidates.append({
                    "symbol": base.get("symbol") or "?",
                    "address": addr,
                    "liquidity": liq,
                    "volume_1h": vol,
                    "age_min": age_min,
                })
        
        candidates = sorted(candidates, key=lambda x: (x.get("age_min") or 9999, -x.get("volume_1h", 0)))[:10]
        
        if not candidates:
            await interaction.followup.send("No candidates above filters.")
            return
        
        embed = discord.Embed(
            title="Fresh Listing Scan (READ-ONLY)",
            description="Display only. Does NOT trigger any buy evaluation.",
            color=discord.Color.gold()
        )
        for c in candidates:
            age = f"{c['age_min']:.0f}m" if c['age_min'] is not None else "?"
            embed.add_field(
                name=c["symbol"],
                value=f"Liq: ${c['liquidity']:,.0f}\nVol1h: ${c['volume_1h']:,.0f}\nAge: {age}\n`{c['address'][:12]}...`",
                inline=True
            )
        
        await interaction.followup.send(embed=embed)
    
    async def _cmd_halt(self, interaction):
        """HALT new buys. Authenticate → DEFER → request_halt() → ack."""
        import discord
        import stop_control
        
        if not self._is_authorized(interaction):
            await self._unauthorized_response(interaction)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            out = await asyncio.to_thread(
                stop_control.request_halt,
                requested_by=f"discord:{interaction.user.id}",
                reason="DISCORD_HALT"
            )
        except Exception as e:
            await interaction.followup.send(f"HALT FAILED: {e}", ephemeral=True)
            return
        
        if out.phase == "HALT_ACTIVE":
            msg = (
                f"**HALT ACTIVE** generation={out.generation}\n"
                "No new buys. Existing positions still managed.\n"
                "In-flight transactions cannot be undone."
            )
            await interaction.followup.send(msg, ephemeral=True)
            
            if "operations" in self.channels:
                await self.channels["operations"].send(
                    f"🛑 **HALT ACTIVE** gen={out.generation} by <@{interaction.user.id}>"
                )
            if "audit-log" in self.channels:
                await self.channels["audit-log"].send(
                    f"HALT ACTIVE gen={out.generation} user={interaction.user.id} "
                    f"ts={datetime.utcnow().isoformat()}"
                )
        else:
            await interaction.followup.send(f"HALT FAILED/DEGRADED: {out.message}", ephemeral=True)
    
    async def _cmd_resume(self, interaction):
        """Resume new buys. Does NOT trigger trading cycle."""
        import discord
        import stop_control
        
        if not self._is_authorized(interaction):
            await self._unauthorized_response(interaction)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            out = await asyncio.to_thread(
                stop_control.request_resume,
                requested_by=f"discord:{interaction.user.id}",
                reason="DISCORD_RESUME"
            )
        except Exception as e:
            await interaction.followup.send(f"RESUME FAILED: {e}", ephemeral=True)
            return
        
        if out.phase == "RESUME_ACTIVE":
            msg = f"**RESUME ACTIVE** generation={out.generation}\nNew buys allowed. Trading cycle not triggered by Discord."
            await interaction.followup.send(msg, ephemeral=True)
            
            if "operations" in self.channels:
                await self.channels["operations"].send(
                    f"✅ **RESUME ACTIVE** gen={out.generation} by <@{interaction.user.id}>"
                )
            if "audit-log" in self.channels:
                await self.channels["audit-log"].send(
                    f"RESUME ACTIVE gen={out.generation} user={interaction.user.id} "
                    f"ts={datetime.utcnow().isoformat()}"
                )
        else:
            await interaction.followup.send(f"RESUME FAILED/DEGRADED: {out.message}", ephemeral=True)
    
    async def _publisher_loop(self):
        """Poll for NEW rows and post to mapped channels. Never modify trading tables."""
        log.info("Event publisher started")
        
        hold_aggregator: dict[str, list] = {}
        last_aggregate_post = time.time()
        
        while self._running:
            try:
                await self._publish_decisions(hold_aggregator)
                await self._publish_trades()
                await self._publish_stop_events()
                
                if time.time() - last_aggregate_post > 300 and hold_aggregator:
                    await self._post_aggregated_holds(hold_aggregator)
                    hold_aggregator.clear()
                    last_aggregate_post = time.time()
                
            except Exception as e:
                log.error(f"Publisher error: {e}")
            
            await asyncio.sleep(10)
    
    async def _publish_decisions(self, hold_aggregator: dict):
        """Publish new decision records."""
        trading_db = ROOT / "data" / "memecoins.db"
        if not trading_db.exists():
            return
        
        last_id, _ = _get_checkpoint("rh_decisions")
        
        try:
            conn = sqlite3.connect(trading_db, timeout=10)
            rows = conn.execute("""
                SELECT id, ts, decision, symbol, asset, security_status, risk_code
                FROM rh_decisions WHERE id > ? ORDER BY id LIMIT 20
            """, (last_id,)).fetchall()
            conn.close()
        except Exception as e:
            log.error(f"Decision query error: {e}")
            return
        
        for row_id, ts, decision, sym, asset, sec, risk in rows:
            decision = decision or "?"
            
            if decision in ("HOLD", "NO_TRADE"):
                key = f"{decision}:{sym or asset}"
                if key not in hold_aggregator:
                    hold_aggregator[key] = []
                hold_aggregator[key].append((ts, risk))
                _set_checkpoint("rh_decisions", row_id, time.time())
                continue
            
            channel_name = None
            if decision == "BUY":
                channel_name = "trade-executions"
            elif decision == "SELL":
                channel_name = "trade-executions"
            elif decision == "BLOCKED":
                channel_name = "risk-vetoes"
                if sec and "goplus" in sec.lower():
                    channel_name = "security"
            
            if channel_name and channel_name in self.channels:
                ts_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "?"
                addr_short = (asset or "")[:12] + "..." if asset else ""
                msg = f"**{decision}** {sym or addr_short} @ {ts_str} | risk={risk or '?'} sec={sec or '?'}"
                try:
                    await self.channels[channel_name].send(msg)
                except Exception as e:
                    log.error(f"Failed to post decision: {e}")
            
            _set_checkpoint("rh_decisions", row_id, time.time())
    
    async def _post_aggregated_holds(self, aggregator: dict):
        """Post aggregated HOLD/NO_TRADE counts."""
        if not aggregator:
            return
        
        channel = self.channels.get("signals")
        if not channel:
            return
        
        summary = []
        for key, items in aggregator.items():
            decision, sym = key.split(":", 1)
            summary.append(f"{decision} {sym}: {len(items)}x")
        
        if summary:
            msg = "**Aggregated signals (5min)**\n" + "\n".join(summary[:20])
            try:
                await channel.send(msg)
            except Exception as e:
                log.error(f"Failed to post aggregated: {e}")
    
    async def _publish_trades(self):
        """Publish new trade executions."""
        trading_db = ROOT / "data" / "memecoins.db"
        if not trading_db.exists():
            return
        
        last_id, _ = _get_checkpoint("rh_live_trades")
        
        try:
            conn = sqlite3.connect(trading_db, timeout=10)
            rows = conn.execute("""
                SELECT id, symbol, address, source, usd_spent, buy_tx, closed_at, 
                       close_reason, pnl_usd, sell_tx
                FROM rh_live_trades WHERE id > ? ORDER BY id LIMIT 10
            """, (last_id,)).fetchall()
            conn.close()
        except Exception as e:
            log.error(f"Trade query error: {e}")
            return
        
        channel = self.channels.get("trade-executions")
        if not channel:
            return
        
        for (row_id, sym, addr, src, usd, buy_tx, closed_at, 
             close_reason, pnl, sell_tx) in rows:
            
            if buy_tx and not closed_at:
                msg = (
                    f"**BUY EXECUTED** {sym or '?'}\n"
                    f"Source: {src} | Amount: ${usd:.2f}\n"
                    f"TX: `{buy_tx}`"
                )
            elif closed_at and sell_tx:
                msg = (
                    f"**SELL EXECUTED** {sym or '?'}\n"
                    f"Reason: {close_reason} | P&L: ${pnl:+.2f}\n"
                    f"TX: `{sell_tx}`"
                )
            else:
                _set_checkpoint("rh_live_trades", row_id, time.time())
                continue
            
            try:
                await channel.send(msg)
            except Exception as e:
                log.error(f"Failed to post trade: {e}")
            
            _set_checkpoint("rh_live_trades", row_id, time.time())
    
    async def _publish_stop_events(self):
        """Publish stop_control events (HALT/RESUME)."""
        import stop_control
        
        try:
            stop_control.ensure_initialized()
        except Exception:
            return
        
        last_id, _ = _get_checkpoint("stop_events")
        
        try:
            conn = sqlite3.connect(stop_control.DB_PATH, timeout=10)
            rows = conn.execute("""
                SELECT rowid, event_type, generation, requested_by, reason
                FROM stop_events WHERE rowid > ? ORDER BY rowid LIMIT 10
            """, (last_id,)).fetchall()
            conn.close()
        except Exception as e:
            log.error(f"Stop events query error: {e}")
            return
        
        ops_channel = self.channels.get("operations")
        audit_channel = self.channels.get("audit-log")
        
        for row_id, evt_type, gen, req_by, reason in rows:
            if evt_type in ("HALT_ACTIVE", "RESUME_ACTIVE"):
                emoji = "🛑" if "HALT" in evt_type else "✅"
                msg = f"{emoji} **{evt_type}** gen={gen} by={req_by} reason={reason}"
                
                if ops_channel:
                    try:
                        await ops_channel.send(msg)
                    except Exception as e:
                        log.error(f"Failed to post stop event: {e}")
                
                if audit_channel:
                    try:
                        await audit_channel.send(
                            f"{evt_type} gen={gen} by={req_by} reason={reason} ts={datetime.utcnow().isoformat()}"
                        )
                    except Exception as e:
                        log.error(f"Failed to post audit: {e}")
            
            _set_checkpoint("stop_events", row_id, time.time())
    
    async def run(self):
        """Run the gateway."""
        if not _enabled():
            print("DISCORD_ENABLED=false — refusing to start gateway")
            print("Set DISCORD_ENABLED=true in .env after setup verification")
            return 1
        
        cfg = self.cfg
        if not cfg["token"]:
            print("DISCORD_BOT_TOKEN missing")
            return 1
        if not cfg["guild_id"] or not cfg["guild_id"].isdigit():
            print("DISCORD_GUILD_ID missing or invalid")
            return 1
        if not cfg["operator_id"] or not cfg["operator_id"].isdigit():
            print("DISCORD_OPERATOR_ID missing or invalid")
            return 1
        
        log.info("Starting Discord gateway...")
        
        try:
            await self.client.start(cfg["token"])
        except Exception as e:
            log.error(f"Gateway error: {e}")
            return 1
        
        return 0
    
    async def shutdown(self):
        """Graceful shutdown."""
        self._running = False
        if self._publisher_task:
            self._publisher_task.cancel()
        await self.client.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Discord operations console gateway")
    p.add_argument("--check", action="store_true", help="Check env/dependencies only")
    p.add_argument("--connect-test", action="store_true", help="Test connection only")
    args = p.parse_args()
    
    if args.check:
        return check()
    
    if args.connect_test:
        return asyncio.run(connect_test())
    
    gateway = DiscordGateway()
    
    try:
        return asyncio.run(gateway.run())
    except KeyboardInterrupt:
        log.info("Shutdown requested")
        asyncio.run(gateway.shutdown())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
