"""Diagnose what's in the project right now."""
import json
import os
from pathlib import Path

print("=" * 60)
print("DIAGNOSTIC")
print("=" * 60)

# 1. What's in backtest/history.py?
h = Path("backtest/history.py").read_text(encoding="utf-8")
print("\n[backtest/history.py] key values:")
for key in [
    'min_history_points: int = 5',
    'min_history_points: int = 24',
    'interval: str = "max"',
    'interval: str = "1h"',
    'interval="max"',
    'interval="1h"',
]:
    marker = "FOUND" if key in h else "missing"
    print(f"  {marker:8s}  {key}")

# 2. What's in resolution_arb.py?
r = Path("strategies/polymarket/resolution_arb.py").read_text(encoding="utf-8")
print("\n[strategies/polymarket/resolution_arb.py] key values:")
for key in [
    'no_end=skip_no_end',
    'skip_no_end = 0',
]:
    marker = "FOUND" if key in r else "missing"
    print(f"  {marker:8s}  {key}")

# 3. What's in the cache folder?
cache = Path("data/cache")
print(f"\n[data/cache/] exists: {cache.exists()}")
if cache.exists():
    files = list(cache.glob("*.json"))
    print(f"  Files: {len(files)}")
    for f in files[:5]:
        try:
            content = json.loads(f.read_text())
            print(f"  {f.name}: {len(content)} history points")
        except Exception as e:
            print(f"  {f.name}: parse error - {e}")
    if len(files) > 5:
        print(f"  ... and {len(files) - 5} more")

# 4. Direct API probe — bypass our code entirely
print("\n[direct API probe] hitting Polymarket Gamma...")
import urllib.request
try:
    with urllib.request.urlopen(
        "https://gamma-api.polymarket.com/markets?closed=true&active=false&limit=3&order=endDate&ascending=false",
        timeout=10,
    ) as resp:
        data = json.loads(resp.read())
    print(f"  Got {len(data)} markets back")
    for m in data[:2]:
        tids = m.get("clobTokenIds")
        if isinstance(tids, str):
            try:
                tids = json.loads(tids)
            except Exception:
                tids = None
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = None
        print(f"  - question:    {(m.get('question') or '?')[:70]}")
        print(f"    endDate:     {m.get('endDate')}")
        print(f"    has tokens:  {bool(tids)} (count={len(tids) if tids else 0})")
        print(f"    has outcomes: {bool(outcomes)}")
        print(f"    outcomePrices: {m.get('outcomePrices')}")
        # Try the prices-history endpoint with this token
        if tids:
            try:
                with urllib.request.urlopen(
                    f"https://clob.polymarket.com/prices-history?market={tids[0]}&interval=max",
                    timeout=10,
                ) as ph:
                    pdata = json.loads(ph.read())
                hist = pdata.get("history", [])
                print(f"    prices-history(max): {len(hist)} points")
            except Exception as e:
                print(f"    prices-history error: {e}")
except Exception as e:
    print(f"  ERROR: {e}")

print("\n" + "=" * 60)
print("Send all of the above output to the assistant.")
print("=" * 60)