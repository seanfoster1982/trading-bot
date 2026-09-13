"""Durable RH NEW-BUY stop control — single source of truth.

HALT = no new buys. Existing SELL/EXIT remain allowed.
Cross-process file lock (Windows LockFileEx / POSIX fcntl) gates NEW BUY broadcast.
No Redis/Kafka/NATS. Never stores secrets or signed raw txs.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
CONTROL_DIR = ROOT / "data" / "control"
DB_PATH = CONTROL_DIR / "rh_stop.sqlite3"
SUBMIT_LOCK = CONTROL_DIR / "submit.lock"
LEGACY_HALT = ROOT / "data" / "cache" / "rh_manual_halt"

MODE_ACTIVE = "ACTIVE"
MODE_HALTED = "HALTED"

EVT_HALT_REQUESTED = "HALT_REQUESTED"
EVT_HALT_ACTIVE = "HALT_ACTIVE"
EVT_RESUME_REQUESTED = "RESUME_REQUESTED"
EVT_RESUME_ACTIVE = "RESUME_ACTIVE"
EVT_BUY_GATE_ALLOWED = "BUY_GATE_ALLOWED"
EVT_BUY_GATE_BLOCKED = "BUY_GATE_BLOCKED"

RES_BROADCAST_ACCEPTED = "BROADCAST_ACCEPTED"
RES_BROADCAST_REJECTED = "BROADCAST_REJECTED"
RES_BROADCAST_UNKNOWN = "BROADCAST_UNKNOWN"
RES_BLOCKED_BY_HALT = "BLOCKED_BY_HALT"


class StopControlError(RuntimeError):
    pass


class FileLock:
    """Cross-process exclusive lock. Never steals on timeout."""

    def __init__(self, path: Path, timeout: float = 30.0):
        self.path = Path(path)
        self.timeout = timeout
        self._fh = None
        self._ov = None
        self._kernel = None
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("xb") as f:
                f.write(b"0")
                f.flush()
                os.fsync(f.fileno())
        self._fh = self.path.open("r+b")
        deadline = time.monotonic() + self.timeout
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

                self._ov = Overlapped()
                self._handle = w.HANDLE(msvcrt.get_osfhandle(self._fh.fileno()))
                self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                self._kernel.LockFileEx.argtypes = [
                    w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(Overlapped)
                ]
                self._kernel.LockFileEx.restype = w.BOOL
                self._kernel.UnlockFileEx.argtypes = [
                    w.HANDLE, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(Overlapped)
                ]
                self._kernel.UnlockFileEx.restype = w.BOOL
                while not self._kernel.LockFileEx(
                    self._handle, 3, 0, 1, 0, ctypes.byref(self._ov)
                ):
                    err = ctypes.get_last_error()
                    if err not in (33, 32):
                        raise StopControlError(f"lock_os_error:{err}")
                    if time.monotonic() >= deadline:
                        raise StopControlError("lock_timeout")
                    time.sleep(0.01)
            else:
                import fcntl

                while True:
                    try:
                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise StopControlError("lock_timeout")
                        time.sleep(0.01)
            return self
        except BaseException:
            self._fh.close()
            self._fh = None
            raise

    def __exit__(self, *exc):
        try:
            if self._fh is None:
                return
            if os.name == "nt":
                if self._kernel and self._handle is not None and self._ov is not None:
                    self._kernel.UnlockFileEx(self._handle, 0, 1, 0, ctypes.byref(self._ov))
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise StopControlError("db_missing")
    uri = "file:" + quote(str(DB_PATH).replace("\\", "/"), safe="/:") + "?mode=rw"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def ensure_initialized(*, prefer_active_if_fresh: bool = True) -> None:
    """Idempotent provision. Fresh: ACTIVE. Legacy halt file → HALTED."""
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    if not SUBMIT_LOCK.exists():
        with SUBMIT_LOCK.open("xb") as f:
            f.write(b"0")
            f.flush()
            os.fsync(f.fileno())
    if DB_PATH.exists():
        return
    legacy = LEGACY_HALT.exists()
    mode = MODE_HALTED if legacy else (MODE_ACTIVE if prefer_active_if_fresh else MODE_HALTED)
    with FileLock(SUBMIT_LOCK, timeout=30):
        if DB_PATH.exists():
            return
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        try:
            conn.executescript(
                """
                CREATE TABLE control_state (
                  control_name TEXT PRIMARY KEY,
                  mode TEXT NOT NULL,
                  generation INTEGER NOT NULL,
                  changed_at REAL NOT NULL,
                  changed_by TEXT,
                  reason TEXT
                );
                CREATE TABLE stop_events (
                  event_id TEXT PRIMARY KEY,
                  timestamp REAL NOT NULL,
                  generation INTEGER NOT NULL,
                  event_type TEXT NOT NULL,
                  requested_by TEXT,
                  previous_mode TEXT,
                  new_mode TEXT,
                  reason TEXT
                );
                CREATE TABLE submissions (
                  attempt_id TEXT PRIMARY KEY,
                  purpose TEXT NOT NULL,
                  asset TEXT,
                  local_tx_hash TEXT,
                  started_at REAL NOT NULL,
                  finished_at REAL,
                  result TEXT
                );
                """
            )
            now = time.time()
            conn.execute(
                "INSERT INTO control_state VALUES (?,?,?,?,?,?)",
                ("rh_new_buy", mode, 1, now, "bootstrap", "initial"),
            )
            conn.commit()
        finally:
            conn.close()
    if mode == MODE_HALTED:
        LEGACY_HALT.parent.mkdir(parents=True, exist_ok=True)
        LEGACY_HALT.write_text("1", encoding="utf-8")
    elif LEGACY_HALT.exists():
        LEGACY_HALT.unlink()


def _audit(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    generation: int,
    requested_by: str | None,
    previous_mode: str | None,
    new_mode: str | None,
    reason: str | None,
) -> str:
    eid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO stop_events VALUES (?,?,?,?,?,?,?,?)",
        (eid, time.time(), generation, event_type, requested_by, previous_mode, new_mode, reason),
    )
    return eid


def snapshot() -> dict:
    ensure_initialized()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM control_state WHERE control_name='rh_new_buy'"
        ).fetchone()
        if not row:
            raise StopControlError("state_missing")
        return dict(row)
    finally:
        conn.close()


def is_new_buy_halted() -> bool:
    try:
        return snapshot()["mode"] == MODE_HALTED
    except (StopControlError, sqlite3.DatabaseError):
        return True  # fail closed on any DB error


@dataclass
class HaltOutcome:
    phase: str
    generation: int
    message: str


def request_halt(*, requested_by: str = "operator", reason: str = "HALT") -> HaltOutcome:
    ensure_initialized()
    try:
        conn = _connect()
    except StopControlError as e:
        return HaltOutcome(
            "HALT_FAILED", 0, f"HALT FAILED / DEGRADED: cannot read state ({e})"
        )
    gen = 0
    try:
        row = conn.execute(
            "SELECT * FROM control_state WHERE control_name='rh_new_buy'"
        ).fetchone()
        prev = row["mode"]
        gen = int(row["generation"])
        _audit(
            conn,
            EVT_HALT_REQUESTED,
            generation=gen,
            requested_by=requested_by,
            previous_mode=prev,
            new_mode=MODE_HALTED,
            reason=reason,
        )
        conn.commit()
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return HaltOutcome(
            "HALT_FAILED",
            0,
            f"HALT FAILED / DEGRADED: cannot write request ({type(e).__name__})",
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

    try:
        with FileLock(SUBMIT_LOCK, timeout=120):
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT * FROM control_state WHERE control_name='rh_new_buy'"
                ).fetchone()
                prev = row["mode"]
                gen = int(row["generation"])
                now = time.time()
                if prev != MODE_HALTED:
                    gen = gen + 1
                    conn.execute(
                        "UPDATE control_state SET mode=?, generation=?, changed_at=?, "
                        "changed_by=?, reason=? WHERE control_name='rh_new_buy'",
                        (MODE_HALTED, gen, now, requested_by, reason),
                    )
                _audit(
                    conn,
                    EVT_HALT_ACTIVE,
                    generation=gen,
                    requested_by=requested_by,
                    previous_mode=prev,
                    new_mode=MODE_HALTED,
                    reason=reason,
                )
                conn.commit()
            finally:
                conn.close()
            LEGACY_HALT.parent.mkdir(parents=True, exist_ok=True)
            LEGACY_HALT.write_text("1", encoding="utf-8")
        return HaltOutcome(
            "HALT_ACTIVE",
            gen,
            f"HALT ACTIVE generation={gen} — no new buys; exits still managed",
        )
    except StopControlError as e:
        return HaltOutcome("HALT_FAILED", 0, f"HALT FAILED / DEGRADED: {e}")


def request_resume(*, requested_by: str = "operator", reason: str = "RESUME") -> HaltOutcome:
    ensure_initialized()
    try:
        with FileLock(SUBMIT_LOCK, timeout=60):
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT * FROM control_state WHERE control_name='rh_new_buy'"
                ).fetchone()
                prev = row["mode"]
                gen = int(row["generation"]) + 1
                now = time.time()
                _audit(
                    conn,
                    EVT_RESUME_REQUESTED,
                    generation=gen,
                    requested_by=requested_by,
                    previous_mode=prev,
                    new_mode=MODE_ACTIVE,
                    reason=reason,
                )
                conn.execute(
                    "UPDATE control_state SET mode=?, generation=?, changed_at=?, "
                    "changed_by=?, reason=? WHERE control_name='rh_new_buy'",
                    (MODE_ACTIVE, gen, now, requested_by, reason),
                )
                _audit(
                    conn,
                    EVT_RESUME_ACTIVE,
                    generation=gen,
                    requested_by=requested_by,
                    previous_mode=prev,
                    new_mode=MODE_ACTIVE,
                    reason=reason,
                )
                conn.commit()
            finally:
                conn.close()
            if LEGACY_HALT.exists():
                LEGACY_HALT.unlink()
        return HaltOutcome("RESUME_ACTIVE", gen, f"RESUME ACTIVE generation={gen}")
    except StopControlError as e:
        return HaltOutcome("HALT_FAILED", 0, f"RESUME FAILED / DEGRADED: {e}")


@dataclass
class BuyAttempt:
    attempt_id: str
    purpose: str = "NEW_BUY"
    asset: str | None = None
    local_tx_hash: str | None = None
    result: str | None = None
    _started: float = 0.0

    def record_result(self, result: str, local_tx_hash: str | None = None) -> None:
        self.result = result
        if local_tx_hash:
            self.local_tx_hash = local_tx_hash
        conn = _connect()
        try:
            conn.execute(
                "UPDATE submissions SET finished_at=?, result=?, "
                "local_tx_hash=COALESCE(?, local_tx_hash) WHERE attempt_id=?",
                (time.time(), result, local_tx_hash, self.attempt_id),
            )
            conn.commit()
        finally:
            conn.close()


@contextlib.contextmanager
def new_buy_gate(*, asset: str | None = None, timeout: float = 60.0) -> Iterator[BuyAttempt | None]:
    """Acquire submission lock; if HALTED yield None (BLOCKED_BY_HALT recorded).

    Caller must perform NEW-BUY approval+broadcast only when attempt is not None,
    entirely inside this context.
    """
    ensure_initialized()
    try:
        lock = FileLock(SUBMIT_LOCK, timeout=timeout)
        lock.__enter__()
    except StopControlError:
        yield None  # fail closed on lock timeout/error
        return
    try:
        try:
            conn = _connect()
        except StopControlError:
            yield None
            return
        attempt: BuyAttempt | None = None
        try:
            row = conn.execute(
                "SELECT * FROM control_state WHERE control_name='rh_new_buy'"
            ).fetchone()
            mode = row["mode"]
            gen = int(row["generation"])
            if mode == MODE_HALTED:
                _audit(
                    conn,
                    EVT_BUY_GATE_BLOCKED,
                    generation=gen,
                    requested_by="buy_gate",
                    previous_mode=mode,
                    new_mode=mode,
                    reason="HALTED",
                )
                aid = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO submissions VALUES (?,?,?,?,?,?,?)",
                    (aid, "NEW_BUY", asset, None, time.time(), time.time(), RES_BLOCKED_BY_HALT),
                )
                conn.commit()
                yield None
                return
            aid = str(uuid.uuid4())
            started = time.time()
            conn.execute(
                "INSERT INTO submissions VALUES (?,?,?,?,?,?,?)",
                (aid, "NEW_BUY", asset, None, started, None, None),
            )
            _audit(
                conn,
                EVT_BUY_GATE_ALLOWED,
                generation=gen,
                requested_by="buy_gate",
                previous_mode=mode,
                new_mode=mode,
                reason="ACTIVE",
            )
            conn.commit()
            attempt = BuyAttempt(attempt_id=aid, asset=asset, _started=started)
        finally:
            conn.close()
        try:
            yield attempt
        finally:
            if attempt is not None and attempt.result is None:
                attempt.record_result(RES_BROADCAST_UNKNOWN)
    finally:
        lock.__exit__(None, None, None)


def classify_broadcast_outcome(txh: str | None, *, error: BaseException | None = None) -> str:
    if txh:
        return RES_BROADCAST_ACCEPTED
    if error is not None:
        name = type(error).__name__.lower()
        msg = str(error).lower()
        if "timeout" in name or "timeout" in msg or "connection" in msg:
            return RES_BROADCAST_UNKNOWN
        return RES_BROADCAST_REJECTED
    return RES_BROADCAST_REJECTED


def main() -> int:
    import argparse
    import json
    import sys

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["status", "halt", "resume", "init"])
    args = p.parse_args()
    if args.action == "init":
        ensure_initialized()
        print(json.dumps(snapshot(), indent=2, default=str))
        return 0
    if args.action == "status":
        ensure_initialized()
        print(json.dumps(snapshot(), indent=2, default=str))
        return 0
    if args.action == "halt":
        out = request_halt(requested_by="cli", reason="CLI HALT")
        print(out.message)
        return 0 if out.phase == "HALT_ACTIVE" else 2
    if args.action == "resume":
        out = request_resume(requested_by="cli", reason="CLI RESUME")
        print(out.message)
        return 0 if out.phase == "RESUME_ACTIVE" else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
