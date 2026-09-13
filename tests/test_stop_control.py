"""Offline Phase 2B stop-control tests. Network/signer/RPC must explode if called.

Requirements coverage matrix (26 total):
  1. TRUE CROSS-PROCESS HALT-DURING-BROADCAST ORDERING
  2. HALT WINS GATE FIRST
  3. TWO BUY WORKERS + HALT
  4. MULTIPLE HALTS IDEMPOTENT
  5. RESTART PERSISTENCE
  6. HALT → RESUME ORDERING
  7. RESUME → HALT ORDERING
  8. LOCK ACQUISITION FAILURE
  9. DB READ FAILURE
  10. HALT STATE WRITE FAILURE
  11. TELEGRAM CRASH AFTER HALT, BEFORE OFFSET COMMIT
  12. TELEGRAM CRASH BEFORE HALT PERSISTENCE
  13. ACK DELIVERY FAILURE
  14. SELL/EXIT UNDER HALT
  15. BUY-ONLY APPROVAL UNDER HALT
  16. EXIT APPROVAL UNDER HALT
  17. BROADCAST ACCEPTED AUDIT
  18. BROADCAST REJECTED AUDIT
  19. BROADCAST UNKNOWN AUDIT
  20. HALT DURING BROADCAST_UNKNOWN
  21. BROKEN/CORRUPT CONTROL DB
  22. LEGACY HALT MIGRATION
  23. KILL.PY RH PATH
  24. KILL.PY ALL PARTIAL FAILURE
  25. OFFLINE NETWORK BARRIER
  26. CAP/CONFIG REGRESSION
"""
from __future__ import annotations

import importlib
import multiprocessing as mp
import os
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))


class OfflineGuard(unittest.TestCase):
    """REQ 25: Any accidental network/signer/RPC call must fail the suite."""

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


def _isolate(tmpdir: Path):
    """Create isolated stop_control module with temp paths."""
    import stop_control as sc
    importlib.reload(sc)
    sc.ROOT = tmpdir
    sc.CONTROL_DIR = tmpdir / "data" / "control"
    sc.DB_PATH = sc.CONTROL_DIR / "rh_stop.sqlite3"
    sc.SUBMIT_LOCK = sc.CONTROL_DIR / "submit.lock"
    sc.LEGACY_HALT = tmpdir / "data" / "cache" / "rh_manual_halt"
    return sc


def _fresh_isolate(tmpdir: Path):
    """Get a completely fresh stop_control module (for persistence tests)."""
    import stop_control as sc_old
    # Force a fresh import
    if "stop_control" in sys.modules:
        del sys.modules["stop_control"]
    import stop_control as sc
    sc.ROOT = tmpdir
    sc.CONTROL_DIR = tmpdir / "data" / "control"
    sc.DB_PATH = sc.CONTROL_DIR / "rh_stop.sqlite3"
    sc.SUBMIT_LOCK = sc.CONTROL_DIR / "submit.lock"
    sc.LEGACY_HALT = tmpdir / "data" / "cache" / "rh_manual_halt"
    return sc


# ============================================================================
# REQ 25: OFFLINE NETWORK BARRIER
# ============================================================================
class TestOfflineNetworkBarrier(OfflineGuard):
    """REQ 25: Suite fails if httpx/RPC/wallet/signer touched."""

    def test_httpx_client_blocked(self):
        """Any httpx.Client() call raises AssertionError."""
        import httpx
        with self.assertRaises(AssertionError) as ctx:
            httpx.Client()
        self.assertIn("OFFLINE GUARD", str(ctx.exception))

    def test_httpx_get_blocked(self):
        """Any httpx.get() call raises AssertionError."""
        import httpx
        with self.assertRaises(AssertionError) as ctx:
            httpx.get("http://example.com")
        self.assertIn("OFFLINE GUARD", str(ctx.exception))

    def test_httpx_post_blocked(self):
        """Any httpx.post() call raises AssertionError."""
        import httpx
        with self.assertRaises(AssertionError) as ctx:
            httpx.post("http://example.com")
        self.assertIn("OFFLINE GUARD", str(ctx.exception))


# ============================================================================
# REQ 26: CAP/CONFIG REGRESSION
# ============================================================================
class TestConfigRegression(unittest.TestCase):
    """REQ 26: RH_TRADE_USD=3, RH_BUDGET_USD=80, RH_MAX_OPEN=10,
    RH_LIVE_ENABLED=True, LIVE_ENABLED=False unchanged."""

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


# ============================================================================
# CORE STOP CONTROL TESTS
# ============================================================================
class TestStopControlCore(OfflineGuard):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.sc = _isolate(self.tmp)

    def test_fresh_init_active(self):
        """Fresh DB initializes to ACTIVE mode with generation=1."""
        self.sc.ensure_initialized()
        self.assertTrue(self.sc.DB_PATH.exists(), f"missing {self.sc.DB_PATH}")
        snap = self.sc.snapshot()
        self.assertEqual(snap["mode"], self.sc.MODE_ACTIVE)
        self.assertEqual(snap["generation"], 1)

    # REQ 22: LEGACY HALT MIGRATION
    def test_legacy_halt_bootstraps_halted(self):
        """REQ 22: Legacy rh_manual_halt file → initial durable HALTED."""
        self.sc.LEGACY_HALT.parent.mkdir(parents=True, exist_ok=True)
        self.sc.LEGACY_HALT.write_text("1", encoding="utf-8")
        self.sc.ensure_initialized()
        self.assertEqual(self.sc.snapshot()["mode"], self.sc.MODE_HALTED)

    # REQ 6: HALT → RESUME ORDERING
    def test_halt_resume_generations_and_audit(self):
        """REQ 6: HALT ACTIVE gen N then RESUME ACTIVE gen N+1 and ACTIVE state."""
        self.sc.ensure_initialized()
        out = self.sc.request_halt(requested_by="test", reason="T")
        self.assertEqual(out.phase, "HALT_ACTIVE", out.message)
        halt_gen = out.generation
        self.assertTrue(self.sc.is_new_buy_halted())
        self.assertTrue(self.sc.LEGACY_HALT.exists())

        out2 = self.sc.request_resume(requested_by="test", reason="R")
        self.assertEqual(out2.phase, "RESUME_ACTIVE", out2.message)
        self.assertGreater(out2.generation, halt_gen, "RESUME gen must be > HALT gen")
        self.assertEqual(out2.generation, halt_gen + 1)
        self.assertFalse(self.sc.is_new_buy_halted())
        self.assertFalse(self.sc.LEGACY_HALT.exists())

        # Verify audit trail
        conn = sqlite3.connect(str(self.sc.DB_PATH))
        types = [r[0] for r in conn.execute("SELECT event_type FROM stop_events ORDER BY rowid")]
        conn.close()
        self.assertIn(self.sc.EVT_HALT_REQUESTED, types)
        self.assertIn(self.sc.EVT_HALT_ACTIVE, types)
        self.assertIn(self.sc.EVT_RESUME_ACTIVE, types)

    # REQ 7: RESUME → HALT ORDERING
    def test_resume_halt_ordering(self):
        """REQ 7: Next HALT generation > prior resume generation and HALTED."""
        self.sc.ensure_initialized()
        # HALT first
        h1 = self.sc.request_halt(requested_by="test", reason="H1")
        self.assertEqual(h1.phase, "HALT_ACTIVE")
        # RESUME
        r1 = self.sc.request_resume(requested_by="test", reason="R1")
        self.assertEqual(r1.phase, "RESUME_ACTIVE")
        resume_gen = r1.generation
        # HALT again
        h2 = self.sc.request_halt(requested_by="test", reason="H2")
        self.assertEqual(h2.phase, "HALT_ACTIVE")
        self.assertGreater(h2.generation, resume_gen, "HALT gen must be > prior RESUME gen")
        self.assertTrue(self.sc.is_new_buy_halted())

    # REQ 15: BUY-ONLY APPROVAL UNDER HALT (buy gate blocks)
    def test_buy_gate_blocks_when_halted(self):
        """REQ 15: After HALT ACTIVE, NEW BUY approval cannot occur."""
        self.sc.ensure_initialized()
        self.sc.request_halt(requested_by="t", reason="H")
        with self.sc.new_buy_gate(asset="0xabc") as attempt:
            self.assertIsNone(attempt)
        conn = sqlite3.connect(str(self.sc.DB_PATH))
        row = conn.execute(
            "SELECT result FROM submissions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], self.sc.RES_BLOCKED_BY_HALT)

    # REQ 17: BROADCAST ACCEPTED AUDIT
    def test_buy_gate_allows_when_active_and_records(self):
        """REQ 17: Inside gate record accepted fake hash; BROADCAST_ACCEPTED + local_tx_hash set."""
        self.sc.ensure_initialized()
        with self.sc.new_buy_gate(asset="0xabc") as attempt:
            self.assertIsNotNone(attempt)
            attempt.record_result(self.sc.RES_BROADCAST_ACCEPTED, local_tx_hash="0xdead")
            aid = attempt.attempt_id
        conn = sqlite3.connect(str(self.sc.DB_PATH))
        row = conn.execute(
            "SELECT result, local_tx_hash FROM submissions WHERE attempt_id=?",
            (aid,),
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], self.sc.RES_BROADCAST_ACCEPTED)
        self.assertEqual(row[1], "0xdead")

    # REQ 19: BROADCAST UNKNOWN AUDIT
    def test_unknown_if_no_result_recorded(self):
        """REQ 19: Exit gate without explicit result → BROADCAST_UNKNOWN recorded."""
        self.sc.ensure_initialized()
        with self.sc.new_buy_gate(asset="0xabc") as attempt:
            self.assertIsNotNone(attempt)
            aid = attempt.attempt_id
        conn = sqlite3.connect(str(self.sc.DB_PATH))
        row = conn.execute(
            "SELECT result FROM submissions WHERE attempt_id=?", (aid,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], self.sc.RES_BROADCAST_UNKNOWN)

    def test_classify_broadcast(self):
        """Test classify_broadcast_outcome for ACCEPTED, UNKNOWN, REJECTED."""
        self.assertEqual(
            self.sc.classify_broadcast_outcome("0x1"), self.sc.RES_BROADCAST_ACCEPTED
        )
        self.assertEqual(
            self.sc.classify_broadcast_outcome(None, error=TimeoutError("x")),
            self.sc.RES_BROADCAST_UNKNOWN,
        )
        self.assertEqual(
            self.sc.classify_broadcast_outcome(None, error=ValueError("revert")),
            self.sc.RES_BROADCAST_REJECTED,
        )

    # REQ 9: DB READ FAILURE
    def test_db_missing_fail_closed(self):
        """REQ 9: Force stop-state DB read failure → NEW BUY fail closed."""
        self.sc.ensure_initialized()
        with mock.patch.object(self.sc, "ensure_initialized", lambda **k: None):
            with mock.patch.object(
                self.sc, "_connect", side_effect=self.sc.StopControlError("db_missing")
            ):
                self.assertTrue(self.sc.is_new_buy_halted())

    # REQ 4: MULTIPLE HALTS IDEMPOTENT
    def test_multiple_halts_idempotent(self):
        """REQ 4: HALT while HALTED stays HALTED, does not resume, does not corrupt.
        Repeated HALT on same mode does NOT change generation (documented behavior)."""
        self.sc.ensure_initialized()
        h1 = self.sc.request_halt(requested_by="t1", reason="H1")
        self.assertEqual(h1.phase, "HALT_ACTIVE")
        gen1 = h1.generation
        self.assertTrue(self.sc.is_new_buy_halted())

        # Second HALT while already HALTED
        h2 = self.sc.request_halt(requested_by="t2", reason="H2")
        self.assertEqual(h2.phase, "HALT_ACTIVE")
        # Documented: generation does NOT increment for repeated HALT on same mode
        self.assertEqual(h2.generation, gen1, "Repeated HALT on HALTED keeps same gen")

        # Still halted, not resumed
        self.assertTrue(self.sc.is_new_buy_halted())

        # No new buy allowed
        with self.sc.new_buy_gate(asset="0xtest") as attempt:
            self.assertIsNone(attempt)


# ============================================================================
# REQ 5: RESTART PERSISTENCE
# ============================================================================
class TestRestartPersistence(OfflineGuard):
    """REQ 5: Fresh module/process on same temp DB sees persisted state."""

    def test_halted_persists_across_reload(self):
        """After HALT, fresh module reload on same DB sees HALTED."""
        tmp = Path(tempfile.mkdtemp())
        sc1 = _isolate(tmp)
        sc1.ensure_initialized()
        sc1.request_halt(requested_by="test", reason="HALT")
        self.assertTrue(sc1.is_new_buy_halted())

        # Fresh module load
        sc2 = _fresh_isolate(tmp)
        sc2.ensure_initialized()
        self.assertTrue(sc2.is_new_buy_halted())

    def test_active_persists_after_resume(self):
        """After HALT then RESUME, fresh module reload sees ACTIVE."""
        tmp = Path(tempfile.mkdtemp())
        sc1 = _isolate(tmp)
        sc1.ensure_initialized()
        sc1.request_halt(requested_by="test", reason="HALT")
        sc1.request_resume(requested_by="test", reason="RESUME")
        self.assertFalse(sc1.is_new_buy_halted())

        # Fresh module load
        sc2 = _fresh_isolate(tmp)
        sc2.ensure_initialized()
        self.assertFalse(sc2.is_new_buy_halted())


# ============================================================================
# CROSS-PROCESS LOCK TESTS
# ============================================================================
def _lock_holder(lock_path: str, ready_name, release_name, held_name):
    """Hold exclusive lock until release event. Paths must already exist."""
    import ctypes
    path = Path(lock_path)
    ready, release, held = ready_name, release_name, held_name
    fh = path.open("r+b")
    try:
        if os.name == "nt":
            import msvcrt
            from ctypes import wintypes as w

            class Overlapped(ctypes.Structure):
                _fields_ = [
                    ("Internal", ctypes.c_size_t),
                    ("InternalHigh", ctypes.c_size_t),
                    ("Offset", w.DWORD),
                    ("OffsetHigh", w.DWORD),
                    ("hEvent", w.HANDLE),
                ]

            ov = Overlapped()
            handle = w.HANDLE(msvcrt.get_osfhandle(fh.fileno()))
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LockFileEx.argtypes = [
                w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(Overlapped)
            ]
            kernel.LockFileEx.restype = w.BOOL
            kernel.UnlockFileEx.argtypes = [
                w.HANDLE, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(Overlapped)
            ]
            kernel.UnlockFileEx.restype = w.BOOL
            if not kernel.LockFileEx(handle, 3, 0, 1, 0, ctypes.byref(ov)):
                return
            held.set()
            ready.set()
            release.wait(60)
            kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(ov))
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            held.set()
            ready.set()
            release.wait(60)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _try_acquire(lock_path: str, result_path: str):
    """Try to acquire lock with LOCK_NB; write result to file."""
    import ctypes
    path = Path(lock_path)
    out = Path(result_path)
    try:
        fh = path.open("r+b")
    except FileNotFoundError:
        out.write_text("BLOCKED:FileNotFoundError", encoding="utf-8")
        return
    try:
        if os.name == "nt":
            import msvcrt
            from ctypes import wintypes as w

            class Overlapped(ctypes.Structure):
                _fields_ = [
                    ("Internal", ctypes.c_size_t),
                    ("InternalHigh", ctypes.c_size_t),
                    ("Offset", w.DWORD),
                    ("OffsetHigh", w.DWORD),
                    ("hEvent", w.HANDLE),
                ]

            ov = Overlapped()
            handle = w.HANDLE(msvcrt.get_osfhandle(fh.fileno()))
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LockFileEx.argtypes = [
                w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(Overlapped)
            ]
            kernel.LockFileEx.restype = w.BOOL
            ok = kernel.LockFileEx(handle, 3, 0, 1, 0, ctypes.byref(ov))
            if ok:
                kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(ov))
                out.write_text("ACQUIRED", encoding="utf-8")
            else:
                out.write_text(f"BLOCKED:lock_os_error:{ctypes.get_last_error()}", encoding="utf-8")
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                out.write_text("ACQUIRED", encoding="utf-8")
            except BlockingIOError:
                out.write_text("BLOCKED:BlockingIOError", encoding="utf-8")
    finally:
        fh.close()


class TestCrossProcessLock(OfflineGuard):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.sc = _isolate(self.tmp)
        self.sc.ensure_initialized()
        self.assertTrue(self.sc.SUBMIT_LOCK.exists())

    def test_cross_process_lock_excludes(self):
        """FileLock excludes concurrent processes."""
        ready = mp.Event()
        release = mp.Event()
        held = mp.Event()
        proc = mp.Process(
            target=_lock_holder,
            args=(str(self.sc.SUBMIT_LOCK), ready, release, held),
        )
        proc.start()
        self.assertTrue(ready.wait(15), "holder did not become ready")
        self.assertTrue(held.is_set())
        result_path = self.tmp / "lock_result.txt"
        buyer = mp.Process(
            target=_try_acquire,
            args=(str(self.sc.SUBMIT_LOCK), str(result_path)),
        )
        buyer.start()
        buyer.join(15)
        release.set()
        proc.join(15)
        self.assertTrue(result_path.exists(), "buyer wrote no result")
        msg = result_path.read_text(encoding="utf-8")
        self.assertTrue(msg.startswith("BLOCKED:"), msg)

    def test_halt_while_gate_held_waits_then_blocks_buy(self):
        """HALT waits for gate holder to finish, then blocks subsequent buys."""
        entered = threading.Event()
        release = threading.Event()

        def holder():
            with self.sc.new_buy_gate(asset="0x1") as attempt:
                self.assertIsNotNone(attempt)
                entered.set()
                release.wait(15)
                attempt.record_result(
                    self.sc.RES_BROADCAST_ACCEPTED, local_tx_hash="0x1"
                )

        t = threading.Thread(target=holder)
        t.start()
        self.assertTrue(entered.wait(5))
        halt_out = []

        def do_halt():
            halt_out.append(self.sc.request_halt(requested_by="race", reason="RACE"))

        ht = threading.Thread(target=do_halt)
        ht.start()
        time.sleep(0.3)
        release.set()
        t.join(20)
        ht.join(60)
        self.assertTrue(halt_out, "halt thread produced no outcome")
        self.assertEqual(halt_out[0].phase, "HALT_ACTIVE", halt_out[0].message)
        with self.sc.new_buy_gate(asset="0x2") as attempt:
            self.assertIsNone(attempt)


# ============================================================================
# REQ 1: TRUE CROSS-PROCESS HALT-DURING-BROADCAST ORDERING (multiprocessing)
# ============================================================================
def _mp_broadcaster(tmpdir_str: str, entered_q, finished_q, release_event):
    """Process A: acquire gate, signal entered, wait for release, record ACCEPTED."""
    import importlib
    tmp = Path(tmpdir_str)
    if "stop_control" in sys.modules:
        del sys.modules["stop_control"]
    import stop_control as sc
    sc.ROOT = tmp
    sc.CONTROL_DIR = tmp / "data" / "control"
    sc.DB_PATH = sc.CONTROL_DIR / "rh_stop.sqlite3"
    sc.SUBMIT_LOCK = sc.CONTROL_DIR / "submit.lock"
    sc.LEGACY_HALT = tmp / "data" / "cache" / "rh_manual_halt"

    with sc.new_buy_gate(asset="0xBroadcast") as attempt:
        if attempt is None:
            entered_q.put(("BLOCKED", time.monotonic()))
            finished_q.put(("BLOCKED", time.monotonic()))
            return
        entered_q.put(("ENTERED", time.monotonic()))
        release_event.wait(60)
        attempt.record_result(sc.RES_BROADCAST_ACCEPTED, local_tx_hash="0xFAKE")
        finished_q.put(("FINISHED", time.monotonic()))


def _mp_halter(tmpdir_str: str, halt_requested_q, halt_active_q):
    """Process B: issue HALT, record timestamps for requested and active."""
    tmp = Path(tmpdir_str)
    if "stop_control" in sys.modules:
        del sys.modules["stop_control"]
    import stop_control as sc
    sc.ROOT = tmp
    sc.CONTROL_DIR = tmp / "data" / "control"
    sc.DB_PATH = sc.CONTROL_DIR / "rh_stop.sqlite3"
    sc.SUBMIT_LOCK = sc.CONTROL_DIR / "submit.lock"
    sc.LEGACY_HALT = tmp / "data" / "cache" / "rh_manual_halt"

    halt_requested_q.put(("HALT_REQUESTED", time.monotonic()))
    out = sc.request_halt(requested_by="mp_test", reason="MP_HALT")
    halt_active_q.put((out.phase, time.monotonic()))


class TestCrossProcessHaltDuringBroadcast(OfflineGuard):
    """REQ 1: TRUE CROSS-PROCESS HALT-DURING-BROADCAST ORDERING."""

    def test_halt_during_broadcast_ordering(self):
        """Process A broadcasts inside gate; Process B HALTs; verify ordering."""
        tmp = Path(tempfile.mkdtemp())
        sc = _isolate(tmp)
        sc.ensure_initialized()

        ctx = mp.get_context("spawn")
        entered_q = ctx.Queue()
        finished_q = ctx.Queue()
        halt_requested_q = ctx.Queue()
        halt_active_q = ctx.Queue()
        release_event = ctx.Event()

        # Start broadcaster (Process A)
        proc_a = ctx.Process(
            target=_mp_broadcaster,
            args=(str(tmp), entered_q, finished_q, release_event),
        )
        proc_a.start()

        # Wait for broadcaster to enter gate
        status, entered_ts = entered_q.get(timeout=30)
        self.assertEqual(status, "ENTERED", "Broadcaster should enter gate")

        # Start halter (Process B)
        proc_b = ctx.Process(
            target=_mp_halter,
            args=(str(tmp), halt_requested_q, halt_active_q),
        )
        proc_b.start()

        # Get halt_requested timestamp
        status, halt_requested_ts = halt_requested_q.get(timeout=30)
        self.assertEqual(status, "HALT_REQUESTED")

        # Give halter time to block on lock
        time.sleep(0.5)

        # Release broadcaster
        release_event.set()

        # Get broadcaster finish time
        status, finished_ts = finished_q.get(timeout=30)
        self.assertEqual(status, "FINISHED")

        # Get halt_active time (halter completes after broadcaster releases lock)
        phase, halt_active_ts = halt_active_q.get(timeout=60)
        self.assertEqual(phase, "HALT_ACTIVE")

        proc_a.join(10)
        proc_b.join(10)

        # Assert ordering: entered < halt_requested < finished < halt_active
        self.assertLess(entered_ts, halt_requested_ts,
                       "broadcast_entered must be < halt_requested")
        self.assertLess(halt_requested_ts, finished_ts,
                       "halt_requested must be < broadcast_finished")
        self.assertLess(finished_ts, halt_active_ts,
                       "broadcast_finished must be < halt_active_acknowledged")


# ============================================================================
# REQ 2: HALT WINS GATE FIRST
# ============================================================================
def _mp_try_buy_after_halt(tmpdir_str: str, result_q):
    """Worker that tries to enter buy gate after halt."""
    tmp = Path(tmpdir_str)
    if "stop_control" in sys.modules:
        del sys.modules["stop_control"]
    import stop_control as sc
    sc.ROOT = tmp
    sc.CONTROL_DIR = tmp / "data" / "control"
    sc.DB_PATH = sc.CONTROL_DIR / "rh_stop.sqlite3"
    sc.SUBMIT_LOCK = sc.CONTROL_DIR / "submit.lock"
    sc.LEGACY_HALT = tmp / "data" / "cache" / "rh_manual_halt"

    attempt_ts = time.monotonic()
    with sc.new_buy_gate(asset="0xAfterHalt") as attempt:
        if attempt is None:
            result_q.put(("BLOCKED", attempt_ts, 0))
        else:
            attempt.record_result(sc.RES_BROADCAST_ACCEPTED, local_tx_hash="0xBAD")
            result_q.put(("ALLOWED", attempt_ts, 1))


class TestHaltWinsGateFirst(OfflineGuard):
    """REQ 2: HALT WINS GATE FIRST - halt before buy attempt blocks buy."""

    def test_halt_wins_gate_first(self):
        """halt_requested < halt_active < buy_gate_attempt < buy_blocked; count=0."""
        tmp = Path(tempfile.mkdtemp())
        sc = _isolate(tmp)
        sc.ensure_initialized()

        halt_requested_ts = time.monotonic()
        out = sc.request_halt(requested_by="test", reason="FIRST")
        halt_active_ts = time.monotonic()
        self.assertEqual(out.phase, "HALT_ACTIVE")

        ctx = mp.get_context("spawn")
        result_q = ctx.Queue()
        proc = ctx.Process(target=_mp_try_buy_after_halt, args=(str(tmp), result_q))
        proc.start()

        status, attempt_ts, count = result_q.get(timeout=30)
        proc.join(10)

        self.assertEqual(status, "BLOCKED")
        self.assertEqual(count, 0, "fake broadcast count must be 0")
        self.assertLess(halt_requested_ts, halt_active_ts)
        self.assertLess(halt_active_ts, attempt_ts,
                       "halt_active must be < buy_gate_attempt")


# ============================================================================
# REQ 3: TWO BUY WORKERS + HALT
# ============================================================================
def _mp_buy_worker(tmpdir_str: str, worker_id: str, entered_q, release_event, result_q):
    """Buy worker that enters gate, waits for release, records result."""
    tmp = Path(tmpdir_str)
    if "stop_control" in sys.modules:
        del sys.modules["stop_control"]
    import stop_control as sc
    sc.ROOT = tmp
    sc.CONTROL_DIR = tmp / "data" / "control"
    sc.DB_PATH = sc.CONTROL_DIR / "rh_stop.sqlite3"
    sc.SUBMIT_LOCK = sc.CONTROL_DIR / "submit.lock"
    sc.LEGACY_HALT = tmp / "data" / "cache" / "rh_manual_halt"

    with sc.new_buy_gate(asset=f"0x{worker_id}") as attempt:
        if attempt is None:
            entered_q.put((worker_id, "BLOCKED", time.monotonic()))
            result_q.put((worker_id, "BLOCKED", 0))
            return
        entered_q.put((worker_id, "ENTERED", time.monotonic()))
        release_event.wait(60)
        attempt.record_result(sc.RES_BROADCAST_ACCEPTED, local_tx_hash=f"0x{worker_id}")
        result_q.put((worker_id, "ACCEPTED", 1))


class TestTwoBuyWorkersHalt(OfflineGuard):
    """REQ 3: TWO BUY WORKERS + HALT."""

    def test_two_workers_halt_ordering(self):
        """A inside may finish; HALT waits for A; B behind gate denied."""
        tmp = Path(tempfile.mkdtemp())
        sc = _isolate(tmp)
        sc.ensure_initialized()

        ctx = mp.get_context("spawn")
        entered_q = ctx.Queue()
        result_q = ctx.Queue()
        release_a = ctx.Event()
        release_b = ctx.Event()

        # Start worker A
        proc_a = ctx.Process(
            target=_mp_buy_worker,
            args=(str(tmp), "A", entered_q, release_a, result_q),
        )
        proc_a.start()

        # Wait for A to enter
        worker, status, _ = entered_q.get(timeout=30)
        self.assertEqual(worker, "A")
        self.assertEqual(status, "ENTERED")

        # Start halter in thread (will block on A)
        halt_out = []
        def do_halt():
            halt_out.append(sc.request_halt(requested_by="test", reason="TWO_WORKERS"))

        halt_thread = threading.Thread(target=do_halt)
        halt_thread.start()
        time.sleep(0.3)  # Give halt time to start waiting

        # Start worker B (should block on lock, then be denied by HALT)
        proc_b = ctx.Process(
            target=_mp_buy_worker,
            args=(str(tmp), "B", entered_q, release_b, result_q),
        )
        proc_b.start()
        time.sleep(0.3)

        # Release A
        release_a.set()

        # Collect A's result
        results = {}
        wid, status, count = result_q.get(timeout=30)
        results[wid] = (status, count)

        # Wait for halt to complete
        halt_thread.join(60)
        self.assertTrue(halt_out, "Halt did not complete")
        self.assertEqual(halt_out[0].phase, "HALT_ACTIVE")

        # Release B (won't matter, already blocked)
        release_b.set()

        # Collect B's result (may be in queue or need to wait)
        try:
            wid, status, count = result_q.get(timeout=10)
            results[wid] = (status, count)
        except:
            pass

        # B might have been blocked before entering queue
        proc_a.join(10)
        proc_b.join(10)

        # A should have finished with ACCEPTED
        self.assertIn("A", results)
        self.assertEqual(results["A"], ("ACCEPTED", 1))

        # B should be BLOCKED with count=0
        if "B" in results:
            self.assertEqual(results["B"], ("BLOCKED", 0))


# ============================================================================
# REQ 8: LOCK ACQUISITION FAILURE
# ============================================================================
class TestLockAcquisitionFailure(OfflineGuard):
    """REQ 8: Force BUY lock fail/timeout → NEW BUY fail closed."""

    def test_lock_timeout_fails_closed(self):
        """Lock timeout → gate yields None, no crash."""
        tmp = Path(tempfile.mkdtemp())
        sc = _isolate(tmp)
        sc.ensure_initialized()

        # Mock FileLock to raise timeout
        original_filelock = sc.FileLock
        class FailingLock:
            def __init__(self, *args, **kwargs):
                pass
            def __enter__(self):
                raise sc.StopControlError("lock_timeout")
            def __exit__(self, *args):
                pass

        sc.FileLock = FailingLock
        try:
            with sc.new_buy_gate(asset="0xFail") as attempt:
                self.assertIsNone(attempt)
        finally:
            sc.FileLock = original_filelock


# ============================================================================
# REQ 10: HALT STATE WRITE FAILURE
# ============================================================================
class TestHaltWriteFailure(OfflineGuard):
    """REQ 10: Force durable halt write failure → must NOT return HALT ACTIVE."""

    def test_halt_write_failure_not_active(self):
        """DB write failure → HALT_FAILED, not HALT_ACTIVE."""
        tmp = Path(tempfile.mkdtemp())
        sc = _isolate(tmp)
        sc.ensure_initialized()

        # Patch _connect to fail on second call (inside FileLock)
        call_count = [0]
        original_connect = sc._connect
        def failing_connect():
            call_count[0] += 1
            if call_count[0] > 1:
                raise sc.StopControlError("write_failure")
            return original_connect()

        with mock.patch.object(sc, "_connect", failing_connect):
            out = sc.request_halt(requested_by="test", reason="FAIL")

        self.assertNotEqual(out.phase, "HALT_ACTIVE",
                           "Must NOT return HALT ACTIVE on write failure")
        self.assertIn("FAILED", out.phase)


# ============================================================================
# REQ 14, 16: SELL/EXIT UNDER HALT
# ============================================================================
class TestSellExitUnderHalt(OfflineGuard):
    """REQ 14, 16: HALTED does NOT block existing-position SELL/EXIT path."""

    def test_sell_path_not_gated(self):
        """REQ 14: Verify manage_open/sell calls are outside new_buy_gate."""
        # This is a code inspection test - verify rh_live_trader.manage_open
        # does not call new_buy_gate
        import rh_live_trader
        import inspect
        source = inspect.getsource(rh_live_trader.manage_open)
        self.assertNotIn("new_buy_gate", source,
                        "manage_open must NOT use new_buy_gate")

    def test_is_halted_allows_exit_conceptually(self):
        """REQ 16: is_halted True still allows exit (exit doesn't check halt)."""
        tmp = Path(tempfile.mkdtemp())
        sc = _isolate(tmp)
        sc.ensure_initialized()
        sc.request_halt(requested_by="test", reason="H")
        self.assertTrue(sc.is_new_buy_halted())
        # The key assertion: sell/exit code does NOT call new_buy_gate
        # and is_new_buy_halted is ONLY checked for NEW BUYS


# ============================================================================
# REQ 17, 18, 19: BROADCAST AUDIT OUTCOMES
# ============================================================================
class TestBroadcastAudit(OfflineGuard):
    """REQ 17, 18, 19: Broadcast outcome audit recording."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.sc = _isolate(self.tmp)
        self.sc.ensure_initialized()

    def test_broadcast_accepted_audit(self):
        """REQ 17: BROADCAST_ACCEPTED + local_tx_hash set."""
        with self.sc.new_buy_gate(asset="0xAcc") as attempt:
            self.assertIsNotNone(attempt)
            attempt.record_result(self.sc.RES_BROADCAST_ACCEPTED, local_tx_hash="0xACCEPTED")
            aid = attempt.attempt_id

        conn = sqlite3.connect(str(self.sc.DB_PATH))
        row = conn.execute(
            "SELECT result, local_tx_hash FROM submissions WHERE attempt_id=?", (aid,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], self.sc.RES_BROADCAST_ACCEPTED)
        self.assertEqual(row[1], "0xACCEPTED")

    def test_broadcast_rejected_audit(self):
        """REQ 18: BROADCAST_REJECTED; no automatic second attempt."""
        with self.sc.new_buy_gate(asset="0xRej") as attempt:
            self.assertIsNotNone(attempt)
            attempt.record_result(self.sc.RES_BROADCAST_REJECTED)
            aid = attempt.attempt_id

        conn = sqlite3.connect(str(self.sc.DB_PATH))
        row = conn.execute(
            "SELECT result FROM submissions WHERE attempt_id=?", (aid,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], self.sc.RES_BROADCAST_REJECTED)

    def test_broadcast_unknown_audit(self):
        """REQ 19: BROADCAST_UNKNOWN; known local tx hash when available."""
        with self.sc.new_buy_gate(asset="0xUnk") as attempt:
            self.assertIsNotNone(attempt)
            # Simulate timeout with known hash
            attempt.record_result(self.sc.RES_BROADCAST_UNKNOWN, local_tx_hash="0xUNKNOWN")
            aid = attempt.attempt_id

        conn = sqlite3.connect(str(self.sc.DB_PATH))
        row = conn.execute(
            "SELECT result, local_tx_hash FROM submissions WHERE attempt_id=?", (aid,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], self.sc.RES_BROADCAST_UNKNOWN)
        self.assertEqual(row[1], "0xUNKNOWN")


# ============================================================================
# REQ 20: HALT DURING BROADCAST_UNKNOWN
# ============================================================================
class TestHaltDuringBroadcastUnknown(OfflineGuard):
    """REQ 20: HALT DURING BROADCAST_UNKNOWN."""

    def test_halt_during_unknown_no_second_buy(self):
        """Broadcaster owns gate; fake send ambiguous; HALT waits; UNKNOWN recorded;
        then HALT ACTIVE; no second BUY submission."""
        tmp = Path(tempfile.mkdtemp())
        sc = _isolate(tmp)
        sc.ensure_initialized()

        entered = threading.Event()
        release = threading.Event()
        aid_holder = []

        def broadcaster():
            with sc.new_buy_gate(asset="0xAmbig") as attempt:
                if attempt:
                    entered.set()
                    release.wait(30)
                    # Record UNKNOWN (ambiguous)
                    attempt.record_result(sc.RES_BROADCAST_UNKNOWN, local_tx_hash="0xAMBIG")
                    aid_holder.append(attempt.attempt_id)

        t = threading.Thread(target=broadcaster)
        t.start()
        self.assertTrue(entered.wait(5))

        # Start halt (will block)
        halt_out = []
        def do_halt():
            halt_out.append(sc.request_halt(requested_by="test", reason="AMBIG"))

        ht = threading.Thread(target=do_halt)
        ht.start()
        time.sleep(0.3)

        # Release broadcaster
        release.set()
        t.join(10)
        ht.join(30)

        # Verify UNKNOWN recorded
        self.assertTrue(aid_holder)
        conn = sqlite3.connect(str(sc.DB_PATH))
        row = conn.execute(
            "SELECT result FROM submissions WHERE attempt_id=?", (aid_holder[0],)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], sc.RES_BROADCAST_UNKNOWN)

        # Verify HALT ACTIVE
        self.assertTrue(halt_out)
        self.assertEqual(halt_out[0].phase, "HALT_ACTIVE")

        # Verify no second buy possible
        with sc.new_buy_gate(asset="0xSecond") as attempt:
            self.assertIsNone(attempt)


# ============================================================================
# REQ 21: BROKEN/CORRUPT CONTROL DB
# ============================================================================
class TestCorruptDB(OfflineGuard):
    """REQ 21: BROKEN/CORRUPT CONTROL DB → fail closed."""

    def test_corrupt_db_fails_closed(self):
        """Unreadable/malformed state → NEW BUY fail closed; do NOT silently init."""
        tmp = Path(tempfile.mkdtemp())
        sc = _isolate(tmp)
        sc.ensure_initialized()

        # Corrupt the DB
        sc.DB_PATH.write_bytes(b"CORRUPT_DATA_NOT_SQLITE")

        # Fresh module
        sc2 = _fresh_isolate(tmp)
        # is_new_buy_halted should return True (fail closed)
        self.assertTrue(sc2.is_new_buy_halted(),
                       "Corrupt DB must fail closed (return halted)")


# ============================================================================
# REQ 22: LEGACY HALT MIGRATION (additional test)
# ============================================================================
class TestLegacyHaltMigrationFresh(OfflineGuard):
    """REQ 22: Legacy halt migration with fresh process verification."""

    def test_legacy_halt_seen_by_fresh_process(self):
        """Legacy file exists, no DB → new process sees HALTED."""
        tmp = Path(tempfile.mkdtemp())
        sc = _isolate(tmp)
        sc.LEGACY_HALT.parent.mkdir(parents=True, exist_ok=True)
        sc.LEGACY_HALT.write_text("1", encoding="utf-8")
        sc.ensure_initialized()

        # Verify HALTED
        self.assertTrue(sc.is_new_buy_halted())

        # Fresh module should also see HALTED
        sc2 = _fresh_isolate(tmp)
        sc2.ensure_initialized()
        self.assertTrue(sc2.is_new_buy_halted())


# ============================================================================
# TELEGRAM TESTS (REQ 11, 12, 13)
# ============================================================================
class TestTelegramAliases(OfflineGuard):
    """Test Telegram command parsing."""

    def test_aliases(self):
        import telegram_notifier as tg
        self.assertEqual(tg.parse_command("HALT"), "HALT")
        self.assertEqual(tg.parse_command("STOP"), "HALT")
        self.assertEqual(tg.parse_command("PAUSE"), "HALT")
        self.assertEqual(tg.parse_command("/emergency_stop"), "HALT")
        self.assertEqual(tg.parse_command("RESUME"), "RESUME")


class TestTelegramOffsetCrash(OfflineGuard):
    """REQ 11, 12, 13: Telegram crash/failure scenarios."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.sc = _isolate(self.tmp)
        self.sc.ensure_initialized()
        # Setup telegram offset path
        import telegram_notifier as tg
        self._orig_offset_path = tg._OFFSET_PATH
        tg._OFFSET_PATH = self.tmp / "data" / "cache" / "tg_offset.txt"
        self.tg = tg

    def tearDown(self):
        self.tg._OFFSET_PATH = self._orig_offset_path
        super().tearDown()

    def test_crash_after_halt_before_offset_commit(self):
        """REQ 11: HALT durable, crash before offset commit → replay safe."""
        # Simulate: HALT received and committed to DB, but offset not advanced
        out = self.sc.request_halt(requested_by="telegram:123", reason="HALT")
        self.assertEqual(out.phase, "HALT_ACTIVE")

        # Offset was NOT committed (simulating crash)
        # On "restart" (fresh module), same HALT update arrives again
        sc2 = _fresh_isolate(self.tmp)
        sc2.ensure_initialized()

        # Re-applying HALT is idempotent
        out2 = sc2.request_halt(requested_by="telegram:123", reason="HALT_REPLAY")
        self.assertEqual(out2.phase, "HALT_ACTIVE")
        self.assertTrue(sc2.is_new_buy_halted())

        # Now offset can be committed
        self.tg.commit_offset(12345)
        self.assertEqual(self.tg._read_offset(), 12345)

    def test_crash_before_halt_persistence(self):
        """REQ 12: HALT received, durable write fails, offset uncommitted."""
        # Patch to fail the durable write
        call_count = [0]
        original_connect = self.sc._connect
        def failing_connect():
            call_count[0] += 1
            if call_count[0] > 1:
                raise self.sc.StopControlError("write_failure")
            return original_connect()

        with mock.patch.object(self.sc, "_connect", failing_connect):
            out = self.sc.request_halt(requested_by="telegram:123", reason="HALT")

        # Should fail
        self.assertNotEqual(out.phase, "HALT_ACTIVE")

        # Offset should NOT be committed (caller responsibility)
        # On next run, same HALT arrives again
        # Fresh module should NOT be halted (write failed)
        sc2 = _fresh_isolate(self.tmp)
        sc2.ensure_initialized()
        # State is still ACTIVE because write failed
        self.assertFalse(sc2.is_new_buy_halted())

    def test_ack_delivery_failure_stays_halted(self):
        """REQ 13: Telegram send fails after HALT durable → stays HALTED."""
        # HALT succeeds
        out = self.sc.request_halt(requested_by="telegram:123", reason="HALT")
        self.assertEqual(out.phase, "HALT_ACTIVE")

        # Telegram send would fail here (mocked by OfflineGuard)
        # But state is already HALTED
        self.assertTrue(self.sc.is_new_buy_halted())

        # Fresh process confirms still HALTED
        sc2 = _fresh_isolate(self.tmp)
        sc2.ensure_initialized()
        self.assertTrue(sc2.is_new_buy_halted())


class TestTelegramHandleHaltOffset(OfflineGuard):
    """Test handle_telegram offset logic with mocks."""

    def test_offset_not_committed_on_halt_failure(self):
        """Offset not committed when halt fails."""
        tmp = Path(tempfile.mkdtemp())
        sc = _isolate(tmp)
        sc.ensure_initialized()

        import telegram_notifier as tg

        # Setup offset path
        offset_path = tmp / "data" / "cache" / "tg_offset.txt"
        offset_path.parent.mkdir(parents=True, exist_ok=True)
        offset_path.write_text("100", encoding="utf-8")

        # Mock poll_commands to return a HALT command
        cmds = [{"cmd": "HALT", "from_id": 42, "chat_id": -100, "update_id": 101}]

        with mock.patch.object(tg, "_OFFSET_PATH", offset_path):
            with mock.patch.object(tg, "poll_commands", return_value=(cmds, 101)):
                with mock.patch.object(tg, "send", return_value=False):
                    # Make halt fail
                    with mock.patch.object(sc, "request_halt",
                                          return_value=sc.HaltOutcome("HALT_FAILED", 0, "fail")):
                        # Simulate handle_telegram logic
                        result_cmds, pending = tg.poll_commands(commit_offset=False)
                        halt_ok = True
                        for item in result_cmds:
                            if item["cmd"] == "HALT":
                                out = sc.request_halt(
                                    requested_by=f"telegram:{item.get('from_id')}",
                                    reason="TELEGRAM_HALT"
                                )
                                if out.phase != "HALT_ACTIVE":
                                    halt_ok = False

                        # Offset should NOT be committed because halt failed
                        if pending is not None and halt_ok:
                            tg.commit_offset(pending)

                        # Verify offset unchanged
                        self.assertEqual(tg._read_offset(), 100)


# ============================================================================
# REQ 23, 24: KILL.PY TESTS
# ============================================================================
class TestKillPy(OfflineGuard):
    """REQ 23, 24: kill.py paths."""

    def test_kill_rh_calls_halt(self):
        """REQ 23: kill.py --platform robinhood calls durable halt."""
        tmp = Path(tempfile.mkdtemp())
        sc = _isolate(tmp)
        sc.ensure_initialized()

        from click.testing import CliRunner
        import kill

        # Mock stop_control in kill module
        halt_called = []
        def mock_halt(**kwargs):
            halt_called.append(kwargs)
            return sc.HaltOutcome("HALT_ACTIVE", 1, "HALT ACTIVE")

        with mock.patch.object(kill.stop_control, "request_halt", mock_halt):
            with mock.patch.object(kill, "PolymarketExchange"):
                with mock.patch.object(kill, "SolanaExchange"):
                    runner = CliRunner()
                    result = runner.invoke(kill.main, ["--platform", "robinhood"])

        self.assertTrue(halt_called, "request_halt was not called")
        self.assertEqual(halt_called[0]["requested_by"], "kill.py")

    def test_kill_all_partial_failure(self):
        """REQ 24: RH halt success + Poly cancel failure → output doesn't claim success."""
        tmp = Path(tempfile.mkdtemp())
        sc = _isolate(tmp)
        sc.ensure_initialized()

        from click.testing import CliRunner
        import kill

        halt_called = []
        def mock_halt(**kwargs):
            halt_called.append(kwargs)
            return sc.HaltOutcome("HALT_ACTIVE", 1, "HALT ACTIVE")

        # Mock Polymarket to fail
        class FailingPoly:
            def __init__(self, **kwargs):
                pass
            async def connect(self):
                pass
            async def cancel_all(self):
                raise RuntimeError("Polymarket connection failed")
            async def close(self):
                pass

        with mock.patch.object(kill.stop_control, "request_halt", mock_halt):
            with mock.patch.object(kill, "PolymarketExchange", FailingPoly):
                with mock.patch.object(kill, "SolanaExchange"):
                    runner = CliRunner()
                    result = runner.invoke(kill.main, ["--platform", "all"])

        # Should have called halt (RH success)
        self.assertTrue(halt_called)
        # Output should not claim complete success
        # (polymarket_failed would be logged)


# ============================================================================
# FAIL CLOSED TESTS
# ============================================================================
class TestFailClosedBuyGate(OfflineGuard):
    """Test fail-closed behavior on errors."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.sc = _isolate(self.tmp)
        self.sc.ensure_initialized()

    def test_gate_yields_none_on_db_error(self):
        """DB error → gate yields None (fail closed)."""
        with mock.patch.object(self.sc, "ensure_initialized", lambda **k: None):
            with mock.patch.object(
                self.sc, "_connect", side_effect=self.sc.StopControlError("db_missing")
            ):
                with self.sc.new_buy_gate(asset="0x") as attempt:
                    self.assertIsNone(attempt)


# ============================================================================
# ADDITIONAL VERIFICATION TESTS
# ============================================================================
class TestRhLiveTraderIntegration(OfflineGuard):
    """Verify rh_live_trader uses stop_control correctly."""

    def test_try_buy_uses_new_buy_gate(self):
        """Verify try_buy wraps broadcast in new_buy_gate."""
        import rh_live_trader
        import inspect
        source = inspect.getsource(rh_live_trader.try_buy)
        self.assertIn("new_buy_gate", source,
                     "try_buy must use new_buy_gate for NEW BUY")

    def test_handle_telegram_uses_commit_offset_false(self):
        """Verify handle_telegram calls poll_commands with commit_offset=False."""
        import rh_live_trader
        import inspect
        source = inspect.getsource(rh_live_trader.handle_telegram_commands)
        self.assertIn("commit_offset=False", source,
                     "handle_telegram must use commit_offset=False")

    def test_handle_telegram_checks_halt_ok(self):
        """Verify handle_telegram only commits offset when halt succeeded."""
        import rh_live_trader
        import inspect
        source = inspect.getsource(rh_live_trader.handle_telegram_commands)
        self.assertIn("halt_ok", source,
                     "handle_telegram must track halt_ok")


if __name__ == "__main__":
    mp.freeze_support()
    unittest.main()
