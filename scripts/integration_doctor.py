"""Integration doctor — status table. Never prints secret values."""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
load_dotenv(ROOT / ".env")


def _present(name: str) -> bool:
    return bool((os.getenv(name) or "").strip().strip('"').strip("'"))


def _yn(ok: bool) -> str:
    return "YES" if ok else "NO"


def _run_smoke(argv: list[str]) -> str:
    try:
        p = subprocess.run(
            [sys.executable, *argv],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = (p.stdout or "") + (p.stderr or "")
        low = out.lower()
        if "install_needed" in low:
            return "INSTALL_NEEDED"
        if "waiting" in low:
            return "WAITING"
        if p.returncode == 0 and ("connected" in low or "pass" in low or "ready" in low):
            return "PASS"
        if p.returncode == 0:
            return "PASS"
        return "FAIL"
    except Exception as e:
        return f"FAIL({type(e).__name__})"


def dexscreener() -> str:
    try:
        r = httpx.get("https://api.dexscreener.com/latest/dex/search", params={"q": "ETH"}, timeout=15.0)
        if r.status_code == 200:
            return "PASS"
        return f"FAIL({r.status_code})"
    except Exception as e:
        return f"FAIL({type(e).__name__})"


def birdeye() -> tuple[str, str]:
    cfg = _present("BIRDEYE_API_KEY")
    if not cfg:
        return "NO", "WAITING"
    # presence-only smoke in doctor to avoid burning quota unless --live-smoke
    return "YES", "PASS(configured)"


def main() -> int:
    rows = []
    # service, plan, configured, connected, live
    rows.append(("DexScreener", "Free", "n/a", dexscreener(), "existing"))
    b_cfg, b_conn = birdeye()
    rows.append(("Birdeye", "Starter", b_cfg, b_conn, "existing"))

    gp_cfg = _yn(_present("GOPLUS_APP_KEY") and _present("GOPLUS_APP_SECRET"))
    gp_en = (os.getenv("GOPLUS_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")
    if gp_cfg == "YES":
        gp_conn = _run_smoke(["scripts/goplus_security.py", "--smoke-test"])
    else:
        gp_conn = "WAITING"
    rows.append(("GoPlus", "Free", gp_cfg, gp_conn, "YES" if gp_en else "NO"))

    rows.append(("DefiLlama", "Free", "n/a", _run_smoke(["scripts/defillama_client.py", "--smoke-test"]), "NO"))

    es_cfg = _yn(_present("ETHERSCAN_API_KEY"))
    es_conn = _run_smoke(["scripts/etherscan_client.py", "--smoke-test"]) if es_cfg == "YES" else "WAITING"
    rows.append(("Etherscan", "Free", es_cfg, es_conn, "NO"))

    ca_cfg = _yn(_present("CHAINABUSE_API_KEY"))
    # do not spend metered call from doctor by default
    ca_conn = "WAITING" if ca_cfg == "NO" else "READY(no metered call)"
    rows.append(("Chainabuse", "Free", ca_cfg, ca_conn, "NO"))

    fb = _run_smoke(["scripts/finbert_sentiment.py", "--smoke-test"])
    rows.append(("FinBERT", "Local", "n/a", fb, "NO"))

    x_cfg = _yn(_present("X_BEARER_TOKEN"))
    x_conn = _run_smoke(["scripts/x_social.py", "--smoke-test"]) if x_cfg == "YES" else "WAITING"
    x_en = (os.getenv("X_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")
    rows.append(("X", "Pay/use", x_cfg, x_conn, "YES" if x_en else "NO"))

    d_cfg = _yn(_present("DISCORD_BOT_TOKEN"))
    d_conn = _run_smoke(["scripts/bootstrap_discord.py", "--check"])
    d_en = (os.getenv("DISCORD_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")
    rows.append(("Discord", "Free", d_cfg, d_conn if d_cfg == "YES" else "WAITING", "YES" if d_en else "NO"))

    rows.append(("Helius", "Existing", _yn(_present("HELIUS_API_KEY")), "n/a", "existing"))
    rows.append(("0x", "Existing", _yn(_present("ZERO_EX_API_KEY")), "n/a", "existing"))
    rows.append(("LunarCrush", "Individual", _yn(_present("LUNARCRUSH_API_KEY")), "DEFERRED", "NO"))
    rows.append(("Solscan Pro", "Lite", "NO", "DEFERRED", "NO"))
    rows.append(("Reddit", "Deferred", "NO", "DEFERRED", "NO"))

    hdr = f"{'INTEGRATION':<14} {'PLAN':<12} {'CONFIGURED':<11} {'CONNECTED':<22} {'LIVE-ENABLED'}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r[0]:<14} {r[1]:<12} {r[2]:<11} {r[3]:<22} {r[4]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
