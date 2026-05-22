from pathlib import Path

p = Path("scripts/check_missing.py")
src = p.read_text(encoding="utf-8")

old = '        print(f"      price=${price:.8f} 1h={ch1h:+.1f}% 4h={ch4h:+.1f}% 24h={ch24:+.1f}%")'

new = (
    '        p_str = f"${price:.8f}" if price is not None else "N/A"\n'
    '        c1 = f"{ch1h:+.1f}%" if ch1h is not None else "N/A"\n'
    '        c4 = f"{ch4h:+.1f}%" if ch4h is not None else "N/A"\n'
    '        c24 = f"{ch24:+.1f}%" if ch24 is not None else "N/A"\n'
    '        print(f"      price={p_str}  1h={c1}  4h={c4}  24h={c24}")'
)

if old in src:
    src = src.replace(old, new)
    p.write_text(src, encoding="utf-8")
    print("Patched line 33 to handle None values")
else:
    print("WARN: line 33 not found in expected form - here is what is there:")
    for i, line in enumerate(src.splitlines(), 1):
        if 31 <= i <= 35:
            print(f"  {i}: {line!r}")
