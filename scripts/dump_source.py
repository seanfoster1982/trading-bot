#!/usr/bin/env python3
"""Dump all source files in this project to a single text file.

Usage:
    python scripts/dump_source.py                    # writes to dist/trading-bot-source.txt
    python scripts/dump_source.py --out path\to\f.txt
    python scripts/dump_source.py --stdout           # print to stdout

Defaults to a path relative to the project root, so it works on any OS
without modification.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".venv", "venv", ".git",
                ".ruff_cache", ".mypy_cache", "dist"}
INCLUDE_EXT = {".py", ".md", ".txt", ".ini"}
INCLUDE_NAMES = {"Makefile", ".env.example", ".gitignore"}


def should_include(p: Path) -> bool:
    parts = set(p.parts)
    if parts & EXCLUDE_DIRS:
        return False
    if "data/cache" in str(p).replace("\\", "/"):
        return False
    return p.suffix in INCLUDE_EXT or p.name in INCLUDE_NAMES


def write_dump(out_stream) -> int:
    files = sorted(
        (p for p in ROOT.rglob("*") if p.is_file() and should_include(p)),
        key=lambda p: str(p.relative_to(ROOT)).lower(),
    )
    out_stream.write("TRADING BOT — FULL SOURCE DUMP\n")
    out_stream.write("=" * 80 + "\n")
    out_stream.write(f"Files: {len(files)}\n\n")

    out_stream.write("TABLE OF CONTENTS\n")
    out_stream.write("-" * 80 + "\n")
    for p in files:
        rel = p.relative_to(ROOT)
        try:
            lines = sum(1 for _ in p.open("r", encoding="utf-8", errors="replace"))
        except Exception:
            lines = 0
        out_stream.write(f"  {rel}  ({lines} lines)\n")
    out_stream.write("\n\n")

    for p in files:
        rel = p.relative_to(ROOT)
        out_stream.write("=" * 80 + "\n")
        out_stream.write(f"FILE: {rel}\n")
        out_stream.write("=" * 80 + "\n")
        try:
            out_stream.write(p.read_text(encoding="utf-8"))
        except Exception as e:
            out_stream.write(f"<could not read: {e}>\n")
        out_stream.write("\n\n")
    return len(files)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        type=Path,
        default=ROOT / "dist" / "trading-bot-source.txt",
        help="Output file path (default: dist/trading-bot-source.txt)",
    )
    ap.add_argument(
        "--stdout",
        action="store_true",
        help="Print to stdout instead of writing a file.",
    )
    args = ap.parse_args()

    if args.stdout:
        n = write_dump(sys.stdout)
        print(f"\n[dumped {n} files to stdout]", file=sys.stderr)
        return

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        n = write_dump(f)
    size = args.out.stat().st_size
    print(f"Wrote {args.out}")
    print(f"  Files dumped: {n}")
    print(f"  Output size:  {size:,} bytes")


if __name__ == "__main__":
    main()
