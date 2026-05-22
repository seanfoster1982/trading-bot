"""Integration tests for paper_trader.open_position.

Seven cases pinning the behavior added in the trading-bot-fixes branch:

  1. Stale signal (older than SIGNAL_TTL_MINUTES) is rejected by
     get_unfilled_buy_signals.
  2. Fresh signal is returned by get_unfilled_buy_signals.
  3. open_position fills at the CURRENT 5m close, not the signal's
     stored entry_price.
  4. stop_loss and take_profit are recomputed around the new entry,
     preserving the signal's absolute risk distance and R:R.
  5. When no current price is available, open_position skips the trade
     and writes nothing.
  6. open_position refuses a strategy that has already hit
     ALLOCATION[strategy]["max_positions"].
  7. The pre-existing capital cap still fires when position size
     exceeds available capital.

Tests never touch data/memecoins.db. Each test uses an isolated
tmp_path sqlite file and monkeypatches paper_trader.DB_PATH to point
at it. The fixture asserts the redirect held so a future refactor
that broke it would fail loudly rather than silently writing into
the live database.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

# paper_trader imports `from strategy import ALLOCATION`, which only works
# when scripts/ is on sys.path. Normal execution gets that for free
# (Python auto-adds the script's directory when you run
# `python scripts/paper_trader.py`); pytest does not, so we add it here.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import paper_trader  # noqa: E402  (sys.path tweak above must come first)


# ----- schema helpers ----------------------------------------------------

SIGNALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    address TEXT NOT NULL,
    action TEXT NOT NULL,
    strategy TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL,
    position_size_usd REAL NOT NULL,
    reasoning_json TEXT NOT NULL,
    indicators_json TEXT NOT NULL,
    generated_at INTEGER NOT NULL,
    executed INTEGER DEFAULT 0
)
"""

# Mirrors the live schema in scripts/compute_indicators.py:init_indicators_db,
# enough columns and NOT-NULL constraints for paper_trader.get_current_price
# to query happily. Extra columns (macd_*, bb_*, ema_*, atr_14, etc.) are
# allowed to be NULL and we don't seed them.
INDICATORS_SCHEMA = """
CREATE TABLE IF NOT EXISTS indicators (
    address TEXT NOT NULL,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    close REAL NOT NULL,
    macd_line REAL,
    macd_signal REAL,
    macd_hist REAL,
    stoch_rsi_k REAL,
    stoch_rsi_d REAL,
    bb_upper REAL,
    bb_middle REAL,
    bb_lower REAL,
    ema_50 REAL,
    ema_200 REAL,
    atr_14 REAL,
    volume_sma_20 REAL,
    obv REAL,
    vwap REAL,
    PRIMARY KEY (address, interval, timestamp)
)
"""


def _seed_signal(
    db_path: Path,
    *,
    symbol: str = "TEST",
    address: str = "addr-test",
    strategy: str = "momentum",
    entry_price: float = 1.00,
    stop_loss: float = 0.95,
    take_profit: float | None = 1.10,
    position_size_usd: float = 10.0,
    generated_at: int | None = None,
    action: str = "BUY",
) -> int:
    """Insert a row into `signals`. Returns the new row id."""
    if generated_at is None:
        generated_at = int(time.time())
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """
        INSERT INTO signals
            (symbol, address, action, strategy, entry_price, stop_loss,
             take_profit, position_size_usd, reasoning_json, indicators_json,
             generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', '{}', ?)
        """,
        (symbol, address, action, strategy, entry_price, stop_loss,
         take_profit, position_size_usd, generated_at),
    )
    conn.commit()
    sig_id = cur.lastrowid
    conn.close()
    return sig_id


def _seed_indicator(
    db_path: Path,
    *,
    address: str,
    close: float,
    symbol: str = "TEST",
    interval: str = "5m",
    timestamp: int | None = None,
) -> None:
    """Insert a 5m close into `indicators` for get_current_price to find."""
    if timestamp is None:
        timestamp = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO indicators (address, symbol, interval, timestamp, close)
        VALUES (?, ?, ?, ?, ?)
        """,
        (address, symbol, interval, timestamp, close),
    )
    conn.commit()
    conn.close()


def _seed_open_position(
    db_path: Path,
    *,
    strategy: str = "momentum",
    symbol: str = "PREV",
    address: str | None = None,
    signal_id: int = 0,
    position_size_usd: float = 5.0,
    entry_price: float = 1.0,
) -> None:
    """Insert a paper_trades row representing a still-open position.
    Used to pre-populate the max_positions test."""
    if address is None:
        address = f"addr-{symbol}"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO paper_trades
            (signal_id, symbol, address, strategy, opened_at,
             entry_price, position_size_usd, tokens_held,
             stop_loss, take_profit)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id, symbol, address, strategy, int(time.time()),
            entry_price, position_size_usd,
            position_size_usd / entry_price,
            entry_price * 0.95, entry_price * 1.10,
        ),
    )
    conn.commit()
    conn.close()


def _count_paper_trades(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    conn.close()
    return int(n)


def _fetch_paper_trade(db_path: Path, signal_id: int) -> dict | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM paper_trades WHERE signal_id = ?", (signal_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ----- fixture -----------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Build a fresh schema in a temp sqlite file and redirect paper_trader
    at it. monkeypatch rolls the DB_PATH change back automatically."""
    db_path = tmp_path / "test_paper.db"
    monkeypatch.setattr(paper_trader, "DB_PATH", db_path)

    # paper_trades schema comes from the module under test; signals +
    # indicators we own here.
    paper_trader.init_paper_db()
    conn = sqlite3.connect(db_path)
    conn.execute(SIGNALS_SCHEMA)
    conn.execute(INDICATORS_SCHEMA)
    conn.commit()
    conn.close()

    # Guard against a refactor that would let the live DB sneak in.
    assert paper_trader.DB_PATH == db_path
    assert "memecoins.db" not in str(paper_trader.DB_PATH)

    yield db_path


# ----- tests -------------------------------------------------------------

def test_stale_signal_is_rejected(tmp_db):
    """A signal older than SIGNAL_TTL_MINUTES must not be returned by
    get_unfilled_buy_signals — replaying yesterday's prices is the bug
    we just fixed."""
    stale_ts = int(time.time()) - (paper_trader.SIGNAL_TTL_MINUTES + 1) * 60
    _seed_signal(tmp_db, generated_at=stale_ts)

    pending = paper_trader.get_unfilled_buy_signals()
    assert pending == []


def test_fresh_signal_is_returned(tmp_db):
    """A signal generated 5 minutes ago is well inside the TTL and must
    be eligible to open."""
    fresh_ts = int(time.time()) - 5 * 60
    sig_id = _seed_signal(tmp_db, generated_at=fresh_ts)

    pending = paper_trader.get_unfilled_buy_signals()
    assert len(pending) == 1
    assert pending[0]["id"] == sig_id


def test_open_fills_at_current_price_not_signal_price(tmp_db):
    """Signal stored entry_price=1.00 but the latest 5m close is 1.10.
    open_position must use 1.10."""
    sig_id = _seed_signal(
        tmp_db,
        address="addr-A",
        entry_price=1.00,
        stop_loss=0.95,
        take_profit=1.10,
        position_size_usd=10.0,
        generated_at=int(time.time()) - 60,
    )
    _seed_indicator(tmp_db, address="addr-A", close=1.10)

    sig = paper_trader.get_unfilled_buy_signals()[0]
    ok, msg = paper_trader.open_position(sig)
    assert ok, f"open_position failed unexpectedly: {msg}"

    pt = _fetch_paper_trade(tmp_db, sig_id)
    assert pt is not None, "paper_trades row not created"
    assert pt["entry_price"] == pytest.approx(1.10)


def test_stop_and_tp_preserve_risk_distance_and_rr(tmp_db):
    """Risk distance = signal entry - signal stop = 0.05.
    R:R = (signal tp - signal entry) / risk = (1.10 - 1.00)/0.05 = 2.0.
    With the fill at 1.10, the new stop must sit 0.05 below it, and the
    new TP must sit 2 * 0.05 = 0.10 above it."""
    sig_id = _seed_signal(
        tmp_db,
        address="addr-B",
        entry_price=1.00,
        stop_loss=0.95,        # absolute risk distance = 0.05
        take_profit=1.10,      # 2R above signal entry
        position_size_usd=10.0,
        generated_at=int(time.time()) - 60,
    )
    _seed_indicator(tmp_db, address="addr-B", close=1.10)

    sig = paper_trader.get_unfilled_buy_signals()[0]
    ok, msg = paper_trader.open_position(sig)
    assert ok, f"open_position failed unexpectedly: {msg}"

    pt = _fetch_paper_trade(tmp_db, sig_id)
    assert pt is not None
    assert pt["entry_price"] == pytest.approx(1.10)
    assert pt["stop_loss"] == pytest.approx(1.05)
    assert pt["take_profit"] == pytest.approx(1.20)


def test_no_current_price_skips_without_writing(tmp_db):
    """No indicators row -> open_position must return (False, ...) AND
    write nothing. Falling back to the stale signal price is exactly
    the bug we fixed; we want fail-closed behavior."""
    _seed_signal(
        tmp_db,
        address="addr-noprice",
        generated_at=int(time.time()) - 60,
    )
    # Intentionally no _seed_indicator call.

    sig = paper_trader.get_unfilled_buy_signals()[0]
    before = _count_paper_trades(tmp_db)
    ok, msg = paper_trader.open_position(sig)
    after = _count_paper_trades(tmp_db)

    assert ok is False
    assert "no current price" in msg.lower()
    assert after == before, "paper_trades row was created despite missing price"


def test_max_positions_cap_blocks_new_open(tmp_db):
    """ALLOCATION['momentum']['max_positions'] == 3. With 3 open momentum
    trades already, a fresh momentum signal must be refused and no new
    paper_trades row written."""
    assert paper_trader.ALLOCATION["momentum"]["max_positions"] == 3, (
        "Test assumes momentum cap is 3; update if ALLOCATION changes."
    )

    for i in range(3):
        _seed_open_position(
            tmp_db, strategy="momentum", symbol=f"PREV{i}",
            address=f"addr-prev-{i}",
        )

    _seed_signal(
        tmp_db,
        symbol="NEW",
        address="addr-new",
        strategy="momentum",
        position_size_usd=10.0,
        generated_at=int(time.time()) - 60,
    )
    _seed_indicator(tmp_db, address="addr-new", close=1.00)

    sig = paper_trader.get_unfilled_buy_signals()[0]
    before = _count_paper_trades(tmp_db)
    ok, msg = paper_trader.open_position(sig)
    after = _count_paper_trades(tmp_db)

    assert ok is False
    assert "max_positions" in msg.lower()
    assert after == before


def test_capital_cap_still_fires(tmp_db):
    """The pre-existing capital cap must still block oversized trades.
    START_CAPITAL_USD == $100; ask for $200 with no other open positions
    and no realized P&L, and open_position must refuse."""
    assert paper_trader.START_CAPITAL_USD == 100.0, (
        "Test assumes START_CAPITAL_USD == 100; update if it changes."
    )

    _seed_signal(
        tmp_db,
        address="addr-bigtrade",
        strategy="momentum",
        position_size_usd=200.0,  # > $100 available capital
        generated_at=int(time.time()) - 60,
    )
    _seed_indicator(tmp_db, address="addr-bigtrade", close=1.00)

    sig = paper_trader.get_unfilled_buy_signals()[0]
    before = _count_paper_trades(tmp_db)
    ok, msg = paper_trader.open_position(sig)
    after = _count_paper_trades(tmp_db)

    assert ok is False
    assert "insufficient capital" in msg.lower()
    assert after == before
