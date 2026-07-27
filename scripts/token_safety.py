"""Token safety gate — a full automated token audit before ANY shadow entry.

Two independent audit layers, both must pass:

LAYER 1 — mint configuration audit (Birdeye token_security):
  Hard blocks (never buy, regardless of score):
    - Non-transferable token (you could buy but never sell — pure honeypot)
    - Freeze authority active (dev can freeze your wallet after you buy)
    - Transfer fee > 5% (tax-style honeypot)
    - Birdeye fakeToken flag

  Scored risks (0-100, higher = more dangerous):
    - Mint authority active (+40): dev can print unlimited supply
    - Transfer fee 1-5% (+15)
    - Top-10 holder concentration, EXCLUDING liquidity pools
      (>70% +30, >50% +20, >35% +10)
    - Creator still holds supply (>30% +35 slow-rug risk, >5% +15)
    - Mutable metadata (+10): dev can rebrand the token after launch
    - Jupiter strict-list membership (-15): externally vetted

LAYER 2 — liquidity & insider audit (RugCheck.xyz public API):
  Hard blocks:
    - Token already flagged as rugged
  Scored risks:
    - Each "danger"-level risk (+15, capped at +45) — e.g. LP unlocked,
      low liquidity, single-holder dominance. Scored rather than hard-blocked
      because every minutes-old launch trips these; the per-strategy score cap
      decides (50 established strategies, 80 fresh listings).
    - Each "warn"-level risk (+5, capped at +15) — e.g. few LP providers
    - Insider wallet network detected by their transaction-graph analysis (+10)
  RugCheck is best-effort: if their API is down we proceed on Layer 1 alone
  (Birdeye unavailable always blocks — no data, no trade).

LAYER 3 (optional) — Token Sniffer (paid API; set TOKENSNIFFER_API_KEY):
  Hard blocks:
    - Flagged as matching a known scam (their contract-similarity database)
    - Sell simulation fails (direct honeypot test)
  Scored risks:
    - Token Sniffer safety score < 30/100 (+30) or < 60/100 (+15)
  Silently skipped when no API key is configured.

Context on Solana "contract review": SPL tokens are instances of the standard
token program, so there is no custom bytecode to audit per token (unlike EVM,
where tools like SolidityScan scan each token's Solidity source). The
rug/honeypot surface here lives in the mint's CONFIGURATION, holder
distribution, and LP status — which is exactly what these two layers audit.

Results are cached in the token_safety table (6h TTL).

CLI for manual checks:
    python scripts/token_safety.py <mint_address>
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(dotenv_path=ROOT / ".env")

DB_PATH = ROOT / "data" / "memecoins.db"
BIRDEYE_BASE = "https://public-api.birdeye.so"
BIRDEYE_KEY = os.getenv("BIRDEYE_API_KEY", "")
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
# Optional third layer — Token Sniffer (paid API, Solana chain id 101).
# Activates automatically when TOKENSNIFFER_API_KEY is added to .env.
TOKENSNIFFER_BASE = "https://tokensniffer.com/api/v2"
TOKENSNIFFER_KEY = os.getenv("TOKENSNIFFER_API_KEY", "")

CACHE_TTL_SEC = 6 * 3600
HARD_BLOCK_FEE_BPS = 500  # >5% transfer fee
# RugCheck marks "Low Liquidity" / "LP Unlocked" / "high ownership" as danger
# on virtually every young token, so its findings are SCORED (per-strategy cap
# decides) rather than hard-blocked; only "already rugged" is absolute.
RUGCHECK_DANGER_SCORE = 15.0
RUGCHECK_DANGER_CAP = 45.0
RUGCHECK_WARN_SCORE = 5.0
RUGCHECK_WARN_CAP = 15.0


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS token_safety (
            address TEXT PRIMARY KEY,
            symbol TEXT,
            score REAL NOT NULL,
            flags TEXT NOT NULL,
            hard_block INTEGER NOT NULL,
            checked_at INTEGER NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def fetch_security(client: httpx.Client, address: str) -> dict | None:
    try:
        r = client.get(
            f"{BIRDEYE_BASE}/defi/token_security",
            headers={"X-API-KEY": BIRDEYE_KEY, "x-chain": "solana",
                     "accept": "application/json"},
            params={"address": address},
            timeout=20.0,
        )
        if r.status_code != 200:
            return None
        return r.json().get("data") or {}
    except Exception:
        return None


def evaluate(sec: dict) -> tuple[float, list[str], bool]:
    """Return (score, flags, hard_block) from a token_security payload."""
    score = 0.0
    flags: list[str] = []
    hard_block = False

    # --- hard blocks: honeypot mechanics ---
    if sec.get("nonTransferable"):
        flags.append("HONEYPOT: token is non-transferable (can buy, cannot sell)")
        hard_block = True
    if sec.get("freezeAuthority"):
        flags.append("FREEZE_AUTHORITY: dev can freeze your wallet after purchase")
        hard_block = True
    if sec.get("fakeToken"):
        flags.append("FAKE_TOKEN: flagged by Birdeye as an impersonation")
        hard_block = True

    fee_bps = 0
    fee_data = sec.get("transferFeeData")
    if isinstance(fee_data, dict):
        fee_bps = int(fee_data.get("transfer_fee_basis_points")
                      or fee_data.get("transferFeeBasisPoints") or 0)
    if fee_bps > HARD_BLOCK_FEE_BPS:
        flags.append(f"HONEYPOT_FEE: {fee_bps / 100:.1f}% transfer fee")
        hard_block = True
    elif fee_bps > 100:
        score += 15
        flags.append(f"TRANSFER_FEE: {fee_bps / 100:.1f}% tax on every trade")

    # --- scored risks ---
    if sec.get("mintAuthority"):
        score += 40
        flags.append("MINT_AUTHORITY: dev can mint unlimited new supply")

    # Concentration excluding LP accounts (fraction 0-1 in the API).
    top10 = sec.get("top10UserPercent")
    if top10 is None:
        top10 = sec.get("top10HolderPercent")
    top10_pct = float(top10 or 0) * 100
    if top10_pct > 70:
        score += 30
        flags.append(f"EXTREME_CONCENTRATION: top 10 wallets hold {top10_pct:.0f}%")
    elif top10_pct > 50:
        score += 20
        flags.append(f"HIGH_CONCENTRATION: top 10 wallets hold {top10_pct:.0f}%")
    elif top10_pct > 35:
        score += 10
        flags.append(f"ELEVATED_CONCENTRATION: top 10 wallets hold {top10_pct:.0f}%")

    creator_pct = float(sec.get("creatorPercentage") or 0) * 100
    if creator_pct > 30:
        # One creator sell nukes the chart — classic slow rug.
        score += 35
        flags.append(f"CREATOR_HOLDS_MAJORITY: creator holds {creator_pct:.1f}% of supply")
    elif creator_pct > 5:
        score += 15
        flags.append(f"CREATOR_HOLDS: {creator_pct:.1f}% of supply")

    if sec.get("mutableMetadata"):
        score += 10
        flags.append("MUTABLE_METADATA: dev can change token identity after launch")

    if sec.get("jupStrictList"):
        score = max(0.0, score - 15)
        flags.append("JUPITER_VETTED: on Jupiter strict list (positive)")

    if sec.get("isToken2022") and not fee_data:
        # Token-2022 without visible fee data — extensions may hide surprises.
        score += 10
        flags.append("TOKEN_2022: extended token program, review extensions")

    return min(100.0, score), flags, hard_block


def fetch_rugcheck(client: httpx.Client, address: str) -> dict | None:
    """Pull the full RugCheck report (public, no key). Best-effort."""
    try:
        r = client.get(
            f"{RUGCHECK_BASE}/tokens/{address}/report",
            headers={"accept": "application/json"},
            timeout=25.0,
        )
        if r.status_code != 200:
            return None
        return r.json() or {}
    except Exception:
        return None


def evaluate_rugcheck(rep: dict) -> tuple[float, list[str], bool]:
    """Return (score, flags, hard_block) from a RugCheck report."""
    score = 0.0
    flags: list[str] = []
    hard_block = False

    if rep.get("rugged"):
        flags.append("RUGCHECK_RUGGED: token already flagged as rugged")
        hard_block = True

    danger_total = 0.0
    warn_total = 0.0
    for risk in rep.get("risks") or []:
        name = risk.get("name") or "unnamed risk"
        level = (risk.get("level") or "").lower()
        if level == "danger":
            danger_total += RUGCHECK_DANGER_SCORE
            flags.append(f"RUGCHECK_DANGER: {name}")
        elif level == "warn":
            warn_total += RUGCHECK_WARN_SCORE
            flags.append(f"RUGCHECK_WARN: {name}")
    score += min(danger_total, RUGCHECK_DANGER_CAP)
    score += min(warn_total, RUGCHECK_WARN_CAP)

    insiders = int(rep.get("graphInsidersDetected") or 0)
    if insiders > 0:
        score += 10
        flags.append(f"INSIDER_NETWORK: {insiders} linked insider wallets detected")

    return score, flags, hard_block


def fetch_tokensniffer(client: httpx.Client, address: str) -> dict | None:
    """Pull a Token Sniffer report (requires paid TOKENSNIFFER_API_KEY)."""
    if not TOKENSNIFFER_KEY:
        return None
    try:
        r = client.get(
            f"{TOKENSNIFFER_BASE}/tokens/101/{address}",
            params={"apikey": TOKENSNIFFER_KEY, "include_metrics": "true",
                    "include_tests": "true"},
            headers={"accept": "application/json"},
            timeout=25.0,
        )
        if r.status_code != 200:
            return None
        data = r.json() or {}
        # Fresh tokens queue for analysis; a pending report has no verdict yet.
        if data.get("status") == "pending":
            return None
        return data
    except Exception:
        return None


def evaluate_tokensniffer(rep: dict) -> tuple[float, list[str], bool]:
    """Return (score, flags, hard_block) from a Token Sniffer report.

    Token Sniffer scores 0-100 where HIGHER = SAFER (inverse of ours).
    Its unique value over the other layers: sell simulation (actual honeypot
    test) and a similarity database of known scam contracts.
    """
    score = 0.0
    flags: list[str] = []
    hard_block = False

    if rep.get("is_flagged"):
        flags.append("TOKENSNIFFER_FLAGGED: matches a known scam")
        hard_block = True

    swap = rep.get("swap_simulation") or {}
    if swap.get("is_sellable") is False:
        flags.append("TOKENSNIFFER_HONEYPOT: sell simulation failed")
        hard_block = True

    ts_score = rep.get("score")
    if ts_score is not None:
        ts_score = float(ts_score)
        if ts_score < 30:
            score += 30
            flags.append(f"TOKENSNIFFER_LOW_SCORE: {ts_score:.0f}/100 (100=safe)")
        elif ts_score < 60:
            score += 15
            flags.append(f"TOKENSNIFFER_MID_SCORE: {ts_score:.0f}/100 (100=safe)")

    return score, flags, hard_block


def check_token(client: httpx.Client, address: str, symbol: str = "?",
                use_cache: bool = True) -> dict | None:
    """Full two-layer audit with caching. Returns dict or None if the primary
    (Birdeye) layer is unavailable. RugCheck is merged in when reachable."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        if use_cache:
            row = conn.execute("""
                SELECT score, flags, hard_block, checked_at FROM token_safety
                WHERE address = ? AND checked_at >= ?
            """, (address, int(time.time()) - CACHE_TTL_SEC)).fetchone()
            if row:
                return {"address": address, "symbol": symbol, "score": row[0],
                        "flags": json.loads(row[1]), "hard_block": bool(row[2]),
                        "cached": True}

        sec = fetch_security(client, address)
        if sec is None:
            return None
        score, flags, hard_block = evaluate(sec)

        rc = fetch_rugcheck(client, address)
        if rc is not None:
            rc_score, rc_flags, rc_hard = evaluate_rugcheck(rc)
            score = min(100.0, score + rc_score)
            flags.extend(rc_flags)
            hard_block = hard_block or rc_hard
        else:
            flags.append("RUGCHECK_UNAVAILABLE: LP/insider audit skipped")

        ts = fetch_tokensniffer(client, address)
        if ts is not None:
            ts_score, ts_flags, ts_hard = evaluate_tokensniffer(ts)
            score = min(100.0, score + ts_score)
            flags.extend(ts_flags)
            hard_block = hard_block or ts_hard

        conn.execute("""
            INSERT OR REPLACE INTO token_safety
            (address, symbol, score, flags, hard_block, checked_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (address, symbol, score, json.dumps(flags), int(hard_block),
              int(time.time())))
        conn.commit()
        return {"address": address, "symbol": symbol, "score": score,
                "flags": flags, "hard_block": hard_block, "cached": False}
    finally:
        conn.close()


def is_safe_to_buy(client: httpx.Client, address: str, symbol: str = "?",
                   max_score: float = 50.0) -> tuple[bool, dict | None]:
    """The gate used by the shadow strategies.

    Blocks on honeypot mechanics regardless of score, otherwise on score.
    If security data is unavailable, blocks (no data = no trade).
    """
    report = check_token(client, address, symbol)
    if report is None:
        return False, None
    if report["hard_block"]:
        return False, report
    return report["score"] <= max_score, report


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/token_safety.py <mint_address>")
        sys.exit(1)
    address = sys.argv[1]
    with httpx.Client() as client:
        report = check_token(client, address, use_cache=False)
    if report is None:
        print("No security data available for this address.")
        sys.exit(1)
    verdict = ("HARD BLOCK — DO NOT BUY" if report["hard_block"]
               else "DANGER" if report["score"] >= 50
               else "CAUTION" if report["score"] >= 25 else "LOOKS SAFE")
    print(f"Address: {address}")
    print(f"Score:   {report['score']:.0f}/100  ->  {verdict}")
    if report["flags"]:
        print("Flags:")
        for f in report["flags"]:
            print(f"  - {f}")
    else:
        print("Flags:   none — clean configuration")


if __name__ == "__main__":
    main()
