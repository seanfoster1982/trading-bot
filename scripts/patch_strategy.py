from pathlib import Path
import sqlite3

# === Part 1: ALTER table to add missing column ===
conn = sqlite3.connect('data/memecoins.db')
cur = conn.cursor()

# Check what columns currently exist
cols = [r[1] for r in cur.execute('PRAGMA table_info(screened_tokens)').fetchall()]
print(f'Current columns: {cols}')

missing = []
if 'exhaustion_score' not in cols:
    missing.append(('exhaustion_score', 'REAL'))
if 'price_change_1h' not in cols:
    missing.append(('price_change_1h', 'REAL'))
if 'price_change_4h' not in cols:
    missing.append(('price_change_4h', 'REAL'))

for col, dtype in missing:
    try:
        cur.execute(f'ALTER TABLE screened_tokens ADD COLUMN {col} {dtype}')
        print(f'Added column: {col}')
    except Exception as e:
        print(f'Could not add {col}: {e}')

conn.commit()
conn.close()

# === Part 2: Fix the strategy docstring escape warning ===
p = Path(r'scripts\strategy.py')
src = p.read_text(encoding='utf-8')
# Use a raw string for the docstring to suppress escape warnings
old_docstring_marker = '70/20/10 allocation: \\\ momentum / \\\ whale copy / \\\ lottery'
new_docstring_marker = '70/20/10 allocation: 70pct momentum / 20pct whale copy / 10pct lottery'
if old_docstring_marker in src:
    src = src.replace(old_docstring_marker, new_docstring_marker)
    print('Fixed docstring escape')
p.write_text(src, encoding='utf-8')
print('PATCHED')
