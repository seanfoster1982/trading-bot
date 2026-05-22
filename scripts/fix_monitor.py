from pathlib import Path

p = Path('scripts/monitor_loop.py')
src = p.read_text(encoding='utf-8')

# Fix 1: Force UTF-8 in subprocess output capture
old_subprocess = """result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )"""
new_subprocess = """result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            encoding='utf-8',
            errors='replace',
            env={**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8'},
        )"""
if old_subprocess in src:
    src = src.replace(old_subprocess, new_subprocess)
    print('Fix 1: UTF-8 subprocess encoding applied')
else:
    print('WARN: subprocess block not found in expected form')

# Fix 2: Escape warnings for \$ in alert format strings
old_buy_line = 'entry={sig[\\\'entry\\\']:.8f} stop={sig[\\\'stop\\\']:.8f} size=\\$' + '{sig[\\\'size\\\']:.2f}'
new_buy_line = 'entry={sig[\\\'entry\\\']:.8f} stop={sig[\\\'stop\\\']:.8f} size=$' + '{sig[\\\'size\\\']:.2f}'
if old_buy_line in src:
    src = src.replace(old_buy_line, new_buy_line)
    print('Fix 2a: BUY alert escape fixed')

old_pnl_line = 'pnl={sign}\\$' + '{cl[\\\'pnl_usd\\\']:.2f}'
new_pnl_line = 'pnl={sign}$' + '{cl[\\\'pnl_usd\\\']:.2f}'
if old_pnl_line in src:
    src = src.replace(old_pnl_line, new_pnl_line)
    print('Fix 2b: PnL alert escape fixed')

p.write_text(src, encoding='utf-8')
print('PATCHED')
