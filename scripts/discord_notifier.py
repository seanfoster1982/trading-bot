"""Discord notifier — webhook/channel posting for Trading Bot alerts.

This module provides send_message() for fire-and-forget Discord notifications.
The gateway (discord_gateway.py) handles persistent bot + slash commands.

DISCORD_ENABLED must be true for send_message to actually post.
If DISCORD_WEBHOOK_URL is set, uses webhook (no bot required).
Otherwise, posts via gateway's channel if available.

Never logs or prints the bot token.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
load_dotenv(ROOT / ".env")


def enabled() -> bool:
    """Check if Discord integration is enabled."""
    return (os.getenv("DISCORD_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")


def configured() -> bool:
    """Check if Discord credentials are configured (token or webhook)."""
    token = (os.getenv("DISCORD_BOT_TOKEN") or "").strip()
    webhook = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
    return bool(token or webhook)


def _webhook_url() -> str:
    """Get webhook URL if configured."""
    return (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()


def send_message(content: str, *, channel: str | None = None, embed: dict | None = None) -> bool:
    """Send a message to Discord.
    
    Args:
        content: Message text (required, max 2000 chars)
        channel: Optional channel hint (not used for webhooks, future use for bot posting)
        embed: Optional Discord embed dict
    
    Returns:
        True if sent successfully, False otherwise.
    
    Note:
        - If DISCORD_ENABLED=false, returns False immediately
        - If DISCORD_WEBHOOK_URL is set, uses webhook
        - Never logs the bot token
    """
    if not enabled():
        return False
    
    if not configured():
        return False
    
    webhook = _webhook_url()
    if webhook:
        return _send_webhook(webhook, content, embed)
    
    return False


def _send_webhook(url: str, content: str, embed: dict | None = None) -> bool:
    """Send via Discord webhook."""
    if not url:
        return False
    
    payload: dict = {"content": content[:2000]}
    if embed:
        payload["embeds"] = [embed]
    
    try:
        r = httpx.post(url, json=payload, timeout=10.0)
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"[discord] webhook failed: {type(e).__name__}", file=sys.stderr)
        return False


def send_embed(
    title: str,
    description: str,
    *,
    color: int = 0x5865F2,
    fields: list[dict] | None = None,
    footer: str | None = None,
    channel: str | None = None,
) -> bool:
    """Send a Discord embed message.
    
    Args:
        title: Embed title
        description: Embed description
        color: Embed color (hex int)
        fields: List of field dicts with name, value, inline keys
        footer: Optional footer text
        channel: Optional channel hint
    
    Returns:
        True if sent successfully.
    """
    embed: dict = {
        "title": title,
        "description": description[:4096],
        "color": color,
    }
    
    if fields:
        embed["fields"] = [
            {"name": f.get("name", "")[:256], "value": f.get("value", "")[:1024], "inline": f.get("inline", False)}
            for f in fields[:25]
        ]
    
    if footer:
        embed["footer"] = {"text": footer[:2048]}
    
    return send_message("", channel=channel, embed=embed)


def send_trade_executed(
    *,
    symbol: str,
    chain: str,
    action: str,
    amount_usd: float,
    strategy: str,
    tx_hash: str | None = None,
) -> bool:
    """Send a trade execution notification."""
    fields = [
        {"name": "Asset", "value": symbol, "inline": True},
        {"name": "Chain", "value": chain, "inline": True},
        {"name": "Action", "value": action, "inline": True},
        {"name": "Amount", "value": f"${amount_usd:.2f}", "inline": True},
        {"name": "Strategy", "value": strategy, "inline": True},
    ]
    
    if tx_hash:
        fields.append({"name": "TX", "value": f"`{tx_hash}`", "inline": False})
    
    color = 0x00FF00 if action.upper() == "BUY" else 0xFF0000
    
    return send_embed(
        title=f"Trade Executed: {action.upper()}",
        description=f"{symbol} on {chain}",
        color=color,
        fields=fields,
        channel="trade-executions",
    )


def send_halt_notification(*, generation: int, requested_by: str) -> bool:
    """Send HALT notification."""
    return send_embed(
        title="🛑 HALT ACTIVE",
        description=f"No new buys. Existing positions still managed.\nIn-flight transactions cannot be undone.",
        color=0xFF0000,
        fields=[
            {"name": "Generation", "value": str(generation), "inline": True},
            {"name": "Requested By", "value": requested_by, "inline": True},
        ],
        channel="operations",
    )


def send_resume_notification(*, generation: int, requested_by: str) -> bool:
    """Send RESUME notification."""
    return send_embed(
        title="✅ RESUME ACTIVE",
        description="New buys allowed.",
        color=0x00FF00,
        fields=[
            {"name": "Generation", "value": str(generation), "inline": True},
            {"name": "Requested By", "value": requested_by, "inline": True},
        ],
        channel="operations",
    )


def send_security_block(
    *,
    symbol: str,
    address: str,
    reason: str,
    source: str = "goplus",
) -> bool:
    """Send security block notification."""
    return send_embed(
        title="🚫 Security Block",
        description=f"{symbol} blocked by {source}",
        color=0xFF6600,
        fields=[
            {"name": "Address", "value": f"`{address[:20]}...`", "inline": True},
            {"name": "Reason", "value": reason, "inline": True},
            {"name": "Source", "value": source, "inline": True},
        ],
        channel="security",
    )


send = send_message
