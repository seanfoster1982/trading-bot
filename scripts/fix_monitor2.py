from pathlib import Path

p = Path("scripts/monitor_loop.py")
lines = p.read_text(encoding="utf-8").splitlines()

# Find the subprocess.run call and inject encoding params
out = []
in_block = False
patched_sub = False
for i, line in enumerate(lines):
    if "result = subprocess.run(" in line and not patched_sub:
        in_block = True
    if in_block and line.strip() == ")":
        # Inject encoding params just before the closing paren
        indent = " " * (len(line) - len(line.lstrip()) + 4)
        out.append(indent + "encoding='utf-8',")
        out.append(indent + "errors='replace',")
        out.append(indent + "env={**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8'},")
        out.append(line)
        in_block = False
        patched_sub = True
        continue
    # Fix the escape warnings: replace \$ with $ in f-strings
    if "size=\\$" in line:
        line = line.replace("size=\\$", "size=$")
    if "pnl={sign}\\$" in line:
        line = line.replace("pnl={sign}\\$", "pnl={sign}$")
    out.append(line)

p.write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"Subprocess UTF-8 patch applied: {patched_sub}")
print("Escape warnings fixed")
