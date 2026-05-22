from pathlib import Path
import ast

p = Path("scripts/wallet_loader.py")
src = p.read_text(encoding="utf-8")

# Replace the wrong expected address with the correct one
old_addr = "EXPECTED_PUBLIC_ADDRESS = 'qfXnLih4u6DExx3Sux2X2gRKwsvDCCcctbsAkL6Hgor'"
new_addr = "EXPECTED_PUBLIC_ADDRESS = '9SDJkC88PgcaAeqLsbE77nnGUne4ZYZqjDHQbGisfGn3'"

if old_addr in src:
    src = src.replace(old_addr, new_addr)
    p.write_text(src, encoding="utf-8")
    print(f"Updated EXPECTED_PUBLIC_ADDRESS")
    print(f"  Old: qfXnLih4u6DExx3Sux2X2gRKwsvDCCcctbsAkL6Hgor")
    print(f"  New: 9SDJkC88PgcaAeqLsbE77nnGUne4ZYZqjDHQbGisfGn3")
else:
    print("WARN: expected address constant not found - manual check needed")

try:
    ast.parse(src)
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
