"""Offline Phase 2B stop-control tests. Network/signer/RPC must explode if called."""
from __future__ import annotations

import multiprocessing as mp
import os
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
    """Any accidental network/signer/RPC call must fail the suite."""

    def setUp(self):
        self._patches = []
        for target in (
            "httpx.Client",
            "httpx.get",
            "httpx.post",
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
    import stop_control as sc
    import importlib

    importlib.reload(sc)
    sc.ROOT = tmpdir
    sc.CONTROL_DIR = tmpdir / "data" / "control"
    sc.DB_PATH = sc.CONTROL_DIR / "rh_stop.sqlite3"
    sc.SUBMIT_LOCK = sc.CONTROL_DIR / "submit.lock"
    sc.LEGACY_HALT = tmpdir / "data" / "cache" / "rh_manual_halt"
    return sc


class TestStopControlCore(OfflineGuard):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.sc = _isolate(self.tmp)

    def test_fresh_init_active(self):
        self.sc.ensure_initialized()
        self.assertTrue(self.sc.DB_PATH.exists(), f"missing {self.sc.DB_PATH}")
        snap = self.sc.snapshot()
        self.assertEqual(snap["mode"], self.sc.MODE_ACTIVE)
        self.assertEqual(snap["generation"], 1)

    def test_legacy_halt_bootstraps_halted(self):
        self.sc.LEGACY_HALT.parent.mkdir(parents=True, exist_ok=True)
        self.sc.LEGACY_HALT.write_text("1", encoding="utf-8")
        self.sc.ensure_initialized()
        self.assertEqual(self.sc.snapshot()["mode"], self.sc.MODE_HALTED)

    def test_halt_resume_generations_and_audit(self):
        self.sc.ensure_initialized()
        out = self.sc.request_halt(requested_by="test", reason="T")
        self.assertEqual(out.phase, "HALT_ACTIVE", out.message)
        self.assertTrue(self.sc.is_new_buy_halted())
        self.assertTrue(self.sc.LEGACY_HALT.exists())
        out2 = self.sc.request_resume(requested_by="test", reason="R")
        self.assertEqual(out2.phase, "RESUME_ACTIVE", out2.message)
        self.assertFalse(self.sc.is_new_buy_halted())
        self.assertFalse(self.sc.LEGACY_HALT.exists())
        conn = sqlite3.connect(str(self.sc.DB_PATH))
        types = [r[0] for r in conn.execute("SELECT event_type FROM stop_events ORDER BY rowid")]
        conn.close()
        self.assertIn(self.sc.EVT_HALT_REQUESTED, types)
        self.assertIn(self.sc.EVT_HALT_ACTIVE, types)
        self.assertIn(self.sc.EVT_RESUME_ACTIVE, types)

    def test_buy_gate_blocks_when_halted(self):
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

    def test_buy_gate_allows_when_active_and_records(self):
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

    def test_unknown_if_no_result_recorded(self):
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

    def test_db_missing_fail_closed(self):
        self.sc.ensure_initialized()
        with mock.patch.object(self.sc, "ensure_initialized", lambda **k: None):
            with mock.patch.object(
                self.sc, "_connect", side_effect=self.sc.StopControlError("db_missing")
            ):
                self.assertTrue(self.sc.is_new_buy_halted())


def _lock_holder(lock_path: str, ready_name, release_name, held_name):
    """Hold exclusive lock until release event. Paths must already exist."""
    import ctypes
    from multiprocessing import Event

    # Events passed as sync managers aren't picklable the same — use Event from args
    ready, release, held = ready_name, release_name, held_name
    path = Path(lock_path)
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
            # FAIL_IMMEDIATELY
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


class TestTelegramAliases(OfflineGuard):
    def test_aliases(self):
        import telegram_notifier as tg

        self.assertEqual(tg.parse_command("HALT"), "HALT")
        self.assertEqual(tg.parse_command("STOP"), "HALT")
        self.assertEqual(tg.parse_command("PAUSE"), "HALT")
        self.assertEqual(tg.parse_command("/emergency_stop"), "HALT")
        self.assertEqual(tg.parse_command("RESUME"), "RESUME")


class TestFailClosedBuyGate(OfflineGuard):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.sc = _isolate(self.tmp)
        self.sc.ensure_initialized()

    def test_gate_yields_none_on_db_error(self):
        with mock.patch.object(self.sc, "ensure_initialized", lambda **k: None):
            with mock.patch.object(
                self.sc, "_connect", side_effect=self.sc.StopControlError("db_missing")
            ):
                with self.sc.new_buy_gate(asset="0x") as attempt:
                    self.assertIsNone(attempt)


if __name__ == "__main__":
    mp.freeze_support()
    unittest.main()
