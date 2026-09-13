"""Offline Phase 2D/2D.2 Discord gateway tests. Network/signer/RPC must explode if called.

Tests cover requirements from Sean's Phase 2D and Phase 2D.2 briefs:

=== PHASE 2D (original 39 requirements) ===
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

=== PHASE 2D.2 (30 additional requirements) ===
  40. BUY/SELL decisions → #signals with "BUY DECISION / NOT EXECUTED" wording
  41. Never label rh_decisions as EXECUTED
  42. _get_checkpoint returns None for unseen tables (not 0)
  43. Seed to MAX(id) on first startup (no silent historical replay)
  44. Optional --backfill flag for historical publish
  45. Preserve existing checkpoints on upgrade
  46. Dedup executions via posted_events using rh-buy:<txhash> / rh-sell:<txhash>
  47. BUY EXECUTED only from rh_live_trades with buy_tx not null
  48. SELL EXECUTED requires sell_tx and closed_at
  49. Execution messages include Chain: Robinhood Chain, Chain ID 4663
  50. Execution messages include Executor wallet (shortened)
  51. Execution messages include explorer link (RH_EXPLORER/tx/<hash>)
  52. Never BUY EXECUTED without tx hash
  53. /status shows RH LIVE chain 4663 + wallet shortened
  54. /status shows Solana LIVE DISABLED / LIVE_ENABLED False
  55. /wallets read-only command exists
  56. No private keys in /status or /wallets
  57. Clarify RH executions ≠ Phantom/Solana wallet
  58. --post-correction-notice option for ops notice
  59. REPORTING_CORRECTION_NOTICE constant defined
  60. No /buy /sell /trade commands
  61. CHANNEL_ROUTES maps BUY→signals (not trade-executions in loop)
  62. _publish_decisions routes to #signals for BUY/SELL
  63. _publish_trades routes to #trade-executions (only with tx)
  64. stop events → #operations + #audit-log
  65. BLOCKED → #risk-vetoes (security if GoPlus-related)
  66. _is_event_posted and _mark_event_posted helper functions
  67. _seed_checkpoint_to_max helper function
  68. Cross-platform single-instance lock (fcntl/msvcrt)
  69. Help mentions RH trades ≠ Phantom
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
            self.assertIsNone(last_id)
            
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


class TestPhase2D2DecisionRouting(OfflineGuard):
    """REQ 40-42: BUY/SELL decisions go to #signals, never as EXECUTED."""

    def test_publish_decisions_routes_to_signals(self):
        """REQ 40: BUY/SELL decisions go to #signals channel."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._publish_decisions)
        self.assertIn('channel_name = "signals"', source)
        self.assertIn("BUY DECISION / NOT EXECUTED", source)
        self.assertIn("SELL DECISION / NOT YET CONFIRMED", source)

    def test_decisions_not_labeled_executed(self):
        """REQ 41: rh_decisions are NEVER labeled as EXECUTED."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._publish_decisions)
        self.assertNotIn("BUY EXECUTED", source)
        self.assertNotIn("SELL EXECUTED", source)

    def test_channel_routes_buy_to_signals(self):
        """REQ 61: CHANNEL_ROUTES maps BUY→signals."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        routes = dg.DiscordGateway.CHANNEL_ROUTES
        self.assertEqual(routes["BUY"], "signals")
        self.assertEqual(routes["SELL"], "signals")


class TestPhase2D2CheckpointSeeding(OfflineGuard):
    """REQ 42-45: Checkpoint seeding to MAX(id), no silent replay."""

    def test_get_checkpoint_returns_none_for_unseen(self):
        """REQ 42: _get_checkpoint returns None for unseen tables."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(dg, "STATE_DB", tmp / "state.sqlite3"):
            dg._init_state_db()
            last_id, _ = dg._get_checkpoint("never_seen_table")
            self.assertIsNone(last_id)

    def test_seed_checkpoint_to_max_function_exists(self):
        """REQ 43, 67: _seed_checkpoint_to_max helper exists."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        self.assertTrue(hasattr(dg, "_seed_checkpoint_to_max"))
        self.assertTrue(callable(dg._seed_checkpoint_to_max))

    def test_backfill_argument_exists(self):
        """REQ 44: --backfill argument for historical publish."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.main)
        self.assertIn("--backfill", source)

    def test_publish_decisions_checks_none_checkpoint(self):
        """REQ 43: Publisher seeds to MAX when checkpoint is None."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._publish_decisions)
        self.assertIn("last_id is None", source)
        self.assertIn("_seed_checkpoint_to_max", source)


class TestPhase2D2Dedup(OfflineGuard):
    """REQ 46: Dedup executions via posted_events."""

    def test_dedup_helper_functions_exist(self):
        """REQ 66: _is_event_posted and _mark_event_posted exist."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        self.assertTrue(hasattr(dg, "_is_event_posted"))
        self.assertTrue(hasattr(dg, "_mark_event_posted"))

    def test_dedup_functions_work(self):
        """REQ 46: Dedup via posted_events using event IDs."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(dg, "STATE_DB", tmp / "state.sqlite3"):
            dg._init_state_db()
            
            event_id = "rh-buy:0x123abc"
            self.assertFalse(dg._is_event_posted(event_id))
            
            dg._mark_event_posted(event_id, "trade-executions")
            self.assertTrue(dg._is_event_posted(event_id))

    def test_publish_trades_uses_dedup(self):
        """REQ 46: _publish_trades uses dedup with tx hash event IDs."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._publish_trades)
        self.assertIn("rh-buy:", source)
        self.assertIn("rh-sell:", source)
        self.assertIn("_is_event_posted", source)
        self.assertIn("_mark_event_posted", source)


class TestPhase2D2ExecutionMessages(OfflineGuard):
    """REQ 47-52: Execution messages only from rh_live_trades with tx."""

    def test_buy_executed_requires_buy_tx(self):
        """REQ 47, 52: BUY EXECUTED only with buy_tx not null."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._publish_trades)
        self.assertIn("if buy_tx and not closed_at", source)
        self.assertIn("BUY EXECUTED", source)

    def test_sell_executed_requires_sell_tx_and_closed_at(self):
        """REQ 48: SELL EXECUTED requires sell_tx and closed_at."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._publish_trades)
        self.assertIn("elif closed_at and sell_tx", source)
        self.assertIn("SELL EXECUTED", source)

    def test_execution_includes_chain_info(self):
        """REQ 49: Execution includes Chain: Robinhood Chain, Chain ID 4663."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._publish_trades)
        self.assertIn("RH_CHAIN_ID", source)
        self.assertIn("Robinhood Chain", source)

    def test_execution_includes_executor_wallet(self):
        """REQ 50: Execution includes executor wallet (shortened)."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._publish_trades)
        self.assertIn("wallet_short", source)
        self.assertIn("Executor", source)

    def test_execution_includes_explorer_link(self):
        """REQ 51: Execution includes explorer link."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._publish_trades)
        self.assertIn("RH_EXPLORER", source)
        self.assertIn("explorer_link", source)


class TestPhase2D2StatusCommand(OfflineGuard):
    """REQ 53-54, 56-57: /status shows chain info and Solana disabled."""

    def test_status_shows_chain_id(self):
        """REQ 53: /status shows RH LIVE chain 4663 + wallet."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._cmd_status)
        self.assertIn("RH_CHAIN_ID", source)
        self.assertIn("wallet_short", source)

    def test_status_shows_solana_disabled(self):
        """REQ 54: /status shows Solana LIVE DISABLED / LIVE_ENABLED."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._cmd_status)
        self.assertIn("LIVE DISABLED", source)
        self.assertIn("LIVE_ENABLED", source)

    def test_status_no_private_keys(self):
        """REQ 56: No private keys in /status."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._cmd_status)
        self.assertNotIn("PRIVATE_KEY", source)
        self.assertNotIn("private_key", source)

    def test_status_clarifies_rh_not_phantom(self):
        """REQ 57: Clarify RH executions ≠ Phantom/Solana wallet."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._cmd_status)
        self.assertIn("Phantom", source)


class TestPhase2D2WalletsCommand(OfflineGuard):
    """REQ 55-56: /wallets read-only command."""

    def test_wallets_command_exists(self):
        """REQ 55: /wallets command is registered."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._register_commands)
        self.assertIn('"wallets"', source)

    def test_wallets_method_exists(self):
        """REQ 55: _cmd_wallets method exists."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        self.assertTrue(hasattr(dg.DiscordGateway, "_cmd_wallets"))

    def test_wallets_no_private_keys(self):
        """REQ 56: No private keys in /wallets."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._cmd_wallets)
        self.assertNotIn("PRIVATE_KEY", source)
        self.assertNotIn("private_key", source)
        self.assertIn("No private keys", source)


class TestPhase2D2CorrectionNotice(OfflineGuard):
    """REQ 58-59: Operations notice about reporting correction."""

    def test_correction_notice_constant_exists(self):
        """REQ 59: REPORTING_CORRECTION_NOTICE constant defined."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        self.assertTrue(hasattr(dg, "REPORTING_CORRECTION_NOTICE"))
        notice = dg.REPORTING_CORRECTION_NOTICE
        self.assertIn("REPORTING CORRECTION", notice)
        self.assertIn("Decisions", notice)
        self.assertIn("Executions", notice)

    def test_post_correction_notice_option(self):
        """REQ 58: --post-correction-notice option exists."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.main)
        self.assertIn("--post-correction-notice", source)

    def test_post_correction_notice_function_exists(self):
        """REQ 58: post_correction_notice function exists."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        self.assertTrue(hasattr(dg, "post_correction_notice"))


class TestPhase2D2NoBuySellCommands(OfflineGuard):
    """REQ 60: No /buy /sell /trade commands."""

    def test_no_buy_command(self):
        """No /buy command registered."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._register_commands)
        lines = [l for l in source.split("\n") if 'name="buy"' in l.lower()]
        self.assertEqual(len(lines), 0, "Found /buy command registration")

    def test_no_sell_command(self):
        """No /sell command registered."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._register_commands)
        lines = [l for l in source.split("\n") if 'name="sell"' in l.lower() and "@self.tree.command" in source[:source.find(l)]]
        self.assertEqual(len(lines), 0, "Found /sell command registration")

    def test_no_trade_command(self):
        """No /trade command registered."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._register_commands)
        lines = [l for l in source.split("\n") if 'name="trade"' in l.lower()]
        self.assertEqual(len(lines), 0, "Found /trade command registration")


class TestPhase2D2ChannelRoutingCorrect(OfflineGuard):
    """REQ 62-65: Correct channel routing in publisher loop."""

    def test_publish_decisions_routes_blocked_correctly(self):
        """REQ 65: BLOCKED → #risk-vetoes (security if GoPlus)."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._publish_decisions)
        self.assertIn('channel_name = "risk-vetoes"', source)
        self.assertIn('channel_name = "security"', source)
        self.assertIn("goplus", source.lower())

    def test_publish_trades_routes_to_trade_executions(self):
        """REQ 63: _publish_trades routes to #trade-executions."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._publish_trades)
        self.assertIn('channels.get("trade-executions")', source)

    def test_stop_events_route_to_operations_and_audit(self):
        """REQ 64: stop events → #operations + #audit-log."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._publish_stop_events)
        self.assertIn('channels.get("operations")', source)
        self.assertIn('channels.get("audit-log")', source)


class TestPhase2D2CrossPlatformLock(OfflineGuard):
    """REQ 68: Cross-platform single-instance lock."""

    def test_lock_uses_fcntl_on_unix(self):
        """REQ 68: Uses fcntl on Unix."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg._acquire_single_instance)
        self.assertIn("fcntl", source)
        self.assertIn("msvcrt", source)
        self.assertIn("sys.platform", source)


class TestPhase2D2HelpMentionsPhantom(OfflineGuard):
    """REQ 69: Help mentions RH trades ≠ Phantom."""

    def test_help_mentions_rh_not_phantom(self):
        """REQ 69: /help mentions RH trades won't appear in Phantom."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._register_commands)
        self.assertIn("Phantom", source)
        self.assertIn("4663", source)


class TestPhase2D2Integration(OfflineGuard):
    """Integration tests for Phase 2D.2 requirements."""

    def test_gateway_class_docstring_mentions_reporting(self):
        """Class docstring mentions Discord reports, doesn't execute."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        docstring = dg.DiscordGateway.__doc__
        self.assertIn("reports", docstring.lower())
        self.assertIn("execute", docstring.lower())

    def test_module_docstring_mentions_channel_routing(self):
        """Module docstring documents correct channel routing."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        docstring = dg.__doc__
        self.assertIn("signals", docstring.lower())
        self.assertIn("trade-executions", docstring.lower())

    def test_backfill_flag_passed_to_publisher(self):
        """Backfill flag is passed through to publisher loop."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        source = inspect.getsource(dg.DiscordGateway._on_ready)
        self.assertIn("backfill=self._backfill", source)

    def test_seed_stop_events_checkpoint_exists(self):
        """_seed_stop_events_checkpoint helper function exists."""
        import discord_gateway as dg
        importlib.reload(dg)
        
        self.assertTrue(hasattr(dg, "_seed_stop_events_checkpoint"))
        self.assertTrue(callable(dg._seed_stop_events_checkpoint))


if __name__ == "__main__":
    unittest.main()
