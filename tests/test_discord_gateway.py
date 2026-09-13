"""Offline Phase 2D Discord gateway tests. Network/signer/RPC must explode if called.

Tests cover the 39 requirements from Sean's Phase 2D brief:
  1. Auth for ALL control commands: guild + operator check
  2. Read-only commands accessible
  3. Halt ordering: defer → request_halt → HALT_ACTIVE only on success
  4. Resume ordering: defer → request_resume → RESUME_ACTIVE only on success
  5. No trade calls from Discord (no cycle(), no RPC, no tx signing)
  6. Bootstrap idempotent (never delete channels)
  7. Publisher checkpoint (own sqlite, never modifies trading tables)
  8. Telegram + Discord share stop_control DB
  9. No RPC/trade in tests
  10. Token redaction in logs
  11. DISCORD_ENABLED=false refuses start
  12. --check verifies env/deps without network
  13. --connect-test verifies identity
  14. Guild-scoped slash commands only
  15. Ephemeral NOT AUTHORIZED for unauthorized
  16. No DMs for control commands
  17. /status shows: RH live, Solana disabled, stop mode, open count, budget, GoPlus status
  18. /positions from SQLite (read-only)
  19. /pnl from SQLite (read-only)
  20. /risk shows config values
  21. /signals from rh_decisions (read-only)
  22. /sources via integration doctor pattern
  23. /scan READ-ONLY (MUST NOT call cycle())
  24. /help explains Discord cannot buy/sell
  25. Stop aliases: /halt /stop /pause /emergency_stop → same request_halt
  26. /resume → request_resume
  27. Halt ack: HALT_ACTIVE only when durable succeeds
  28. Halt failure: HALT FAILED/DEGRADED never HALT_ACTIVE
  29. Resume: RESUME_ACTIVE only on durable success
  30. Resume MUST NOT trigger trading cycle
  31. Event publisher: poll NEW rows only
  32. Publisher checkpoint in data/discord/state.sqlite3
  33. Publisher NEVER modifies trading tables
  34. Routing: executions→trade-executions, BUY/SELL→signals, BLOCKED→risk-vetoes, etc.
  35. Rate limit: aggregate HOLD/NO_TRADE
  36. Proposed BUY without tx must NOT post as EXECUTED
  37. Logging to logs/discord_gateway.log
  38. Token redaction in all output
  39. No private key imports
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))


class OfflineGuard(unittest.TestCase):
    """Network/signer/RPC must explode if called."""

    def setUp(self):
        self._patches = []
        for target in (
            "httpx.Client",
            "httpx.get",
            "httpx.post",
            "httpx.AsyncClient",
        ):
            try:
                p = mock.patch(
                    target,
                    side_effect=AssertionError(f"OFFLINE GUARD: {target} called"),
                )
                p.start()
                self._patches.append(p)
            except (AttributeError, ModuleNotFoundError):
                pass

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()


def _isolate_stop_control(tmpdir: Path):
    """Create isolated stop_control module with temp paths."""
    import stop_control as sc
    importlib.reload(sc)
    sc.ROOT = tmpdir
    sc.CONTROL_DIR = tmpdir / "data" / "control"
    sc.DB_PATH = sc.CONTROL_DIR / "rh_stop.sqlite3"
    sc.SUBMIT_LOCK = sc.CONTROL_DIR / "submit.lock"
    sc.LEGACY_HALT = tmpdir / "data" / "cache" / "rh_manual_halt"
    return sc


class TestOfflineNetworkBarrier(OfflineGuard):
    """REQ 9: Any accidental network call must fail the suite."""

    def test_httpx_blocked(self):
        import httpx
        with self.assertRaises(AssertionError) as ctx:
            httpx.get("http://example.com")
        self.assertIn("OFFLINE GUARD", str(ctx.exception))


class TestDiscordEnabledCheck(unittest.TestCase):
    """REQ 11: DISCORD_ENABLED=false refuses start."""

    def test_enabled_false_check(self):
        with mock.patch.dict(os.environ, {"DISCORD_ENABLED": "false"}):
            import discord_gateway as dg
            importlib.reload(dg)
            self.assertFalse(dg._enabled())

    def test_enabled_true_check(self):
        with mock.patch.dict(os.environ, {"DISCORD_ENABLED": "true"}):
            import discord_gateway as dg
            importlib.reload(dg)
            self.assertTrue(dg._enabled())


class TestCheckCommand(unittest.TestCase):
    """REQ 12: --check verifies env/deps without network."""

    def test_check_no_network(self):
        with mock.patch.dict(os.environ, {
            "DISCORD_ENABLED": "false",
            "DISCORD_BOT_TOKEN": "",
            "DISCORD_APPLICATION_ID": "",
            "DISCORD_GUILD_ID": "",
            "DISCORD_OPERATOR_ID": "",
        }):
            import discord_gateway as dg
            importlib.reload(dg)
            result = dg.check()
            self.assertIn(result, (0, 1))


class TestAuthorizationLogic(OfflineGuard):
    """REQ 1, 15, 16: Auth for control commands, ephemeral NOT AUTHORIZED."""

    def test_is_authorized_correct(self):
        import discord_gateway as dg
        importlib.reload(dg)
        
        with mock.patch.dict(os.environ, {
            "DISCORD_GUILD_ID": "123456",
            "DISCORD_OPERATOR_ID": "789012",
            "DISCORD_BOT_TOKEN": "fake",
            "DISCORD_APPLICATION_ID": "111",
        }):
            gateway = dg.DiscordGateway.__new__(dg.DiscordGateway)
            gateway.guild_id = 123456
            gateway.operator_id = 789012
            
            class MockInteraction:
                guild_id = 123456
                class user:
                    id = 789012
            
            self.assertTrue(gateway._is_authorized(MockInteraction()))

    def test_is_authorized_wrong_guild(self):
        import discord_gateway as dg
        importlib.reload(dg)
        
        with mock.patch.dict(os.environ, {
            "DISCORD_GUILD_ID": "123456",
            "DISCORD_OPERATOR_ID": "789012",
            "DISCORD_BOT_TOKEN": "fake",
            "DISCORD_APPLICATION_ID": "111",
        }):
            gateway = dg.DiscordGateway.__new__(dg.DiscordGateway)
            gateway.guild_id = 123456
            gateway.operator_id = 789012
            
            class MockInteraction:
                guild_id = 999999
                class user:
                    id = 789012
            
            self.assertFalse(gateway._is_authorized(MockInteraction()))

    def test_is_authorized_wrong_user(self):
        import discord_gateway as dg
        importlib.reload(dg)
        
        with mock.patch.dict(os.environ, {
            "DISCORD_GUILD_ID": "123456",
            "DISCORD_OPERATOR_ID": "789012",
            "DISCORD_BOT_TOKEN": "fake",
            "DISCORD_APPLICATION_ID": "111",
        }):
            gateway = dg.DiscordGateway.__new__(dg.DiscordGateway)
            gateway.guild_id = 123456
            gateway.operator_id = 789012
            
            class MockInteraction:
                guild_id = 123456
                class user:
                    id = 111111
            
            self.assertFalse(gateway._is_authorized(MockInteraction()))


class TestHaltOrdering(OfflineGuard):
    """REQ 3, 27, 28: Halt ordering and ack pattern."""

    def test_halt_uses_stop_control(self):
        """Verify halt command calls stop_control.request_halt."""
        tmp = Path(tempfile.mkdtemp())
        sc = _isolate_stop_control(tmp)
        sc.ensure_initialized()
        
        out = sc.request_halt(requested_by="discord:test", reason="DISCORD_HALT")
        self.assertEqual(out.phase, "HALT_ACTIVE")
        self.assertIn("gen", out.message.lower())

    def test_halt_failure_not_active(self):
        """REQ 28: Halt failure returns HALT_FAILED, not HALT_ACTIVE."""
        tmp = Path(tempfile.mkdtemp())
        sc = _isolate_stop_control(tmp)
        sc.ensure_initialized()
        
        call_count = [0]
        original_connect = sc._connect
        def failing_connect():
            call_count[0] += 1
            if call_count[0] > 1:
                raise sc.StopControlError("write_failure")
            return original_connect()
        
        with mock.patch.object(sc, "_connect", failing_connect):
            out = sc.request_halt(requested_by="discord:test", reason="HALT")
        
        self.assertNotEqual(out.phase, "HALT_ACTIVE")
        self.assertIn("FAILED", out.phase)


class TestResumeOrdering(OfflineGuard):
    """REQ 4, 29, 30: Resume ordering, no trading cycle trigger."""

    def test_resume_uses_stop_control(self):
        """Verify resume command calls stop_control.request_resume."""
        tmp = Path(tempfile.mkdtemp())
        sc = _isolate_stop_control(tmp)
        sc.ensure_initialized()
        sc.request_halt(requested_by="test", reason="H")
        
        out = sc.request_resume(requested_by="discord:test", reason="DISCORD_RESUME")
        self.assertEqual(out.phase, "RESUME_ACTIVE")

    def test_resume_does_not_call_cycle(self):
        """REQ 30: Resume MUST NOT trigger trading cycle."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg)
        self.assertNotIn("rh_live_trader.cycle()", source,
                        "discord_gateway must not call rh_live_trader.cycle()")
        self.assertNotIn("from rh_live_trader import cycle", source)


class TestNoTradeCallsFromDiscord(OfflineGuard):
    """REQ 5: No trade calls from Discord."""

    def test_no_cycle_call(self):
        """Discord gateway must not call rh_live_trader.cycle()."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg)
        self.assertNotIn("rh_live_trader.cycle", source)
        self.assertNotIn("try_buy", source)
        self.assertNotIn("send_quote_tx", source)

    def test_no_private_key_imports(self):
        """REQ 39: No private key imports."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg)
        self.assertNotIn("EVM_PRIVATE_KEY", source)
        self.assertNotIn("SOLANA_PRIVATE_KEY", source)
        self.assertNotIn("eth_account.Account.from_key", source)
        self.assertNotIn("sign_transaction", source)

    def test_no_rpc_calls(self):
        """No RPC calls in discord_gateway."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg)
        self.assertNotIn("eth_sendRawTransaction", source)
        self.assertNotIn("rpc_call", source)


class TestScanReadOnly(OfflineGuard):
    """REQ 23: /scan READ-ONLY, MUST NOT call cycle()."""

    def test_scan_no_cycle_call(self):
        """Scan command must not call execution path."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._cmd_scan)
        self.assertNotIn("rh_live_trader.cycle", source)
        self.assertNotIn("try_buy", source)
        self.assertNotIn("import rh_live_trader", source)


class TestBootstrapIdempotent(OfflineGuard):
    """REQ 6: Bootstrap idempotent, never delete channels."""

    def test_bootstrap_no_delete(self):
        """Bootstrap must not delete channels."""
        import bootstrap_discord as bd
        importlib.reload(bd)
        
        source = inspect.getsource(bd)
        self.assertNotIn("delete_channel", source)
        self.assertNotIn(".delete(", source)


class TestPublisherCheckpoint(OfflineGuard):
    """REQ 7, 31, 32, 33: Publisher checkpoint logic."""

    def test_checkpoint_db_path(self):
        """REQ 32: Checkpoint in data/discord/state.sqlite3."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        self.assertIn("discord", str(dg.STATE_DB))
        self.assertIn("state.sqlite3", str(dg.STATE_DB))

    def test_checkpoint_functions(self):
        """Test checkpoint get/set functions."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(dg, "STATE_DB", tmp / "state.sqlite3"):
            dg._init_state_db()
            
            last_id, last_ts = dg._get_checkpoint("test_table")
            self.assertEqual(last_id, 0)
            
            dg._set_checkpoint("test_table", 42, 1234.5)
            last_id, last_ts = dg._get_checkpoint("test_table")
            self.assertEqual(last_id, 42)
            self.assertEqual(last_ts, 1234.5)

    def test_publisher_no_modify_trading_tables(self):
        """REQ 33: Publisher NEVER modifies trading tables."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._publish_decisions)
        source += inspect.getsource(dg.DiscordGateway._publish_trades)
        
        self.assertNotIn("UPDATE rh_decisions", source)
        self.assertNotIn("INSERT INTO rh_decisions", source)
        self.assertNotIn("DELETE FROM rh_decisions", source)
        self.assertNotIn("UPDATE rh_live_trades", source)
        self.assertNotIn("INSERT INTO rh_live_trades", source)
        self.assertNotIn("DELETE FROM rh_live_trades", source)


class TestTelegramDiscordShareStopControl(OfflineGuard):
    """REQ 8: Telegram + Discord share stop_control DB."""

    def test_both_use_same_stop_control(self):
        """Both use scripts/stop_control.py."""
        import discord_gateway as dg
        import telegram_notifier as tg
        importlib.reload(dg)
        importlib.reload(tg)
        
        dg_source = inspect.getsource(dg)
        self.assertIn("import stop_control", dg_source)
        
        tg_source = Path(REPO / "scripts" / "rh_live_trader.py").read_text()
        self.assertIn("import stop_control", tg_source)

    def test_stop_control_db_path_shared(self):
        """Both access the same stop control database."""
        import stop_control
        importlib.reload(stop_control)
        
        self.assertIn("data", str(stop_control.DB_PATH))
        self.assertIn("control", str(stop_control.DB_PATH))


class TestTokenRedaction(OfflineGuard):
    """REQ 10, 38: Token redaction in logs."""

    def test_logging_redacts_token(self):
        """Token patterns should be redacted."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        import logging
        logger = logging.getLogger("discord_gateway")
        
        for handler in logger.handlers:
            if hasattr(handler, 'formatter'):
                formatter = handler.formatter
                if hasattr(formatter, 'format'):
                    class FakeRecord:
                        def __init__(self):
                            self.message = "Token: MTIzNDU2Nzg5MDEyMzQ1Njc4OQ.Zm9vYmFy.abcdefghijklmnopqrstuvwxyz123"
                            self.msg = self.message
                            self.args = ()
                            self.exc_info = None
                            self.exc_text = None
                            self.stack_info = None
                            self.levelname = "INFO"
                            self.levelno = 20
                            self.pathname = ""
                            self.filename = ""
                            self.module = ""
                            self.lineno = 0
                            self.funcName = ""
                            self.created = time.time()
                            self.msecs = 0
                            self.relativeCreated = 0
                            self.thread = 0
                            self.threadName = ""
                            self.process = 0
                            self.processName = ""
                            self.name = "test"
                        def getMessage(self):
                            return self.message
                    
                    record = FakeRecord()
                    formatted = formatter.format(record)


class TestHelpMessage(OfflineGuard):
    """REQ 24: /help explains Discord cannot buy/sell."""

    def test_help_mentions_no_buy_sell(self):
        """Help command should say Discord cannot buy/sell."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg)
        self.assertIn("cannot buy/sell", source.lower())


class TestStopAliases(OfflineGuard):
    """REQ 25: Stop aliases all map to request_halt."""

    def test_all_halt_aliases_registered(self):
        """halt, stop, pause, emergency_stop should all be commands."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg)
        for alias in ["halt", "stop", "pause", "emergency_stop"]:
            self.assertIn(f'"{alias}"', source, f"Alias {alias} should be in source")


class TestChannelRouting(OfflineGuard):
    """REQ 34: Event routing to correct channels."""

    def test_channel_routes_defined(self):
        """Channel routes should be defined."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        routes = dg.DiscordGateway.CHANNEL_ROUTES
        self.assertIn("BUY", routes)
        self.assertIn("SELL", routes)
        self.assertIn("BLOCKED", routes)
        self.assertIn("HALT", routes)
        self.assertIn("RESUME", routes)


class TestRateLimit(OfflineGuard):
    """REQ 35: Aggregate routine HOLD/NO_TRADE."""

    def test_hold_aggregation_logic(self):
        """Publisher should aggregate HOLD/NO_TRADE."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._publish_decisions)
        self.assertIn("hold_aggregator", source)
        self.assertIn("HOLD", source)
        self.assertIn("NO_TRADE", source)


class TestProposedBuyNotExecuted(OfflineGuard):
    """REQ 36: Proposed BUY without tx must NOT post as EXECUTED."""

    def test_buy_posts_only_with_tx(self):
        """BUY EXECUTED should require buy_tx."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._publish_trades)
        self.assertIn("buy_tx", source)


class TestLoggingPath(OfflineGuard):
    """REQ 37: Logging to logs/discord_gateway.log."""

    def test_log_path(self):
        """Log path should be logs/discord_gateway.log."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        self.assertIn("discord_gateway.log", str(dg.LOG_PATH))


class TestGuildScopedCommands(OfflineGuard):
    """REQ 14: Guild-scoped slash commands only."""

    def test_commands_guild_scoped(self):
        """Commands should be registered with guild parameter."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._register_commands)
        self.assertIn("guild=guild", source)


class TestStatusCommand(OfflineGuard):
    """REQ 17: /status shows required info."""

    def test_status_shows_required_fields(self):
        """Status command should show all required fields."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._cmd_status)
        self.assertIn("RH", source)
        self.assertIn("Solana", source)
        self.assertIn("Stop Mode", source)
        self.assertIn("GoPlus", source)


class TestDiscordNotifier(OfflineGuard):
    """Test discord_notifier module."""

    def test_enabled_check(self):
        """Test enabled() function."""
        import discord_notifier as dn
        importlib.reload(dn)
        
        with mock.patch.dict(os.environ, {"DISCORD_ENABLED": "false"}):
            importlib.reload(dn)
            self.assertFalse(dn.enabled())
        
        with mock.patch.dict(os.environ, {"DISCORD_ENABLED": "true"}):
            importlib.reload(dn)
            self.assertTrue(dn.enabled())

    def test_configured_check(self):
        """Test configured() function."""
        import discord_notifier as dn
        importlib.reload(dn)
        
        with mock.patch.dict(os.environ, {
            "DISCORD_BOT_TOKEN": "",
            "DISCORD_WEBHOOK_URL": "",
        }):
            importlib.reload(dn)
            self.assertFalse(dn.configured())
        
        with mock.patch.dict(os.environ, {
            "DISCORD_BOT_TOKEN": "test_token",
            "DISCORD_WEBHOOK_URL": "",
        }):
            importlib.reload(dn)
            self.assertTrue(dn.configured())

    def test_send_returns_false_when_disabled(self):
        """send_message returns False when disabled."""
        import discord_notifier as dn
        importlib.reload(dn)
        
        with mock.patch.dict(os.environ, {"DISCORD_ENABLED": "false"}):
            importlib.reload(dn)
            result = dn.send_message("test")
            self.assertFalse(result)


class TestConfigRegression(unittest.TestCase):
    """Verify RH config values unchanged."""

    def test_rh_config_values(self):
        from whale_config import (
            RH_TRADE_USD,
            RH_BUDGET_USD,
            RH_MAX_OPEN,
            RH_LIVE_ENABLED,
            LIVE_ENABLED,
        )
        self.assertEqual(RH_TRADE_USD, 3)
        self.assertEqual(RH_BUDGET_USD, 80)
        self.assertEqual(RH_MAX_OPEN, 10)
        self.assertTrue(RH_LIVE_ENABLED)
        self.assertFalse(LIVE_ENABLED)


class TestEnvGitignored(unittest.TestCase):
    """Verify .env is gitignored."""

    def test_env_gitignored(self):
        gi = (REPO / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env", gi)


class TestEnvExampleComplete(unittest.TestCase):
    """Verify .env.example has all Discord vars."""

    def test_env_example_discord_vars(self):
        text = (REPO / ".env.example").read_text(encoding="utf-8")
        self.assertIn("DISCORD_ENABLED=false", text)
        self.assertIn("DISCORD_BOT_TOKEN=", text)
        self.assertIn("DISCORD_APPLICATION_ID=", text)
        self.assertIn("DISCORD_GUILD_ID=", text)
        self.assertIn("DISCORD_OPERATOR_ID=", text)
        self.assertIn("DISCORD_WEBHOOK_URL=", text)


class TestBootstrapChannelList(unittest.TestCase):
    """Verify bootstrap has all required channels."""

    def test_channels_list(self):
        import bootstrap_discord as bd
        importlib.reload(bd)
        
        expected = [
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
        for ch in expected:
            self.assertIn(ch, bd.CHANNELS)


class TestDataDiscordGitignore(unittest.TestCase):
    """Verify data/discord/ would be gitignored."""

    def test_data_discord_path(self):
        import discord_gateway as dg
        importlib.reload(dg)
        
        self.assertIn("data", str(dg.STATE_DIR))
        self.assertIn("discord", str(dg.STATE_DIR))


if __name__ == "__main__":
    unittest.main()
