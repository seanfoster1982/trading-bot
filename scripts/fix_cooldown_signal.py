from pathlib import Path
import ast

p = Path("scripts/strategy.py")
src = p.read_text(encoding="utf-8")

old_block = """    # COOLDOWN CHECK: skip if this token had a STOP_LOSS within last 24h
    in_cd, hours_remaining = recent_stoploss_cooldown(address)
    if in_cd:
        return Signal(
            address=address, symbol=symbol, action='HOLD',
            strategy={'A': 'momentum', 'B': 'whale_copy', 'C': 'lottery'}.get(screen, 'momentum'),
            entry_price=0.0, stop_loss=0.0, take_profit=0.0,
            position_size_usd=0.0,
            reasoning=f'COOLDOWN: stop-out within {COOLDOWN_HOURS_AFTER_STOPLOSS}h ({hours_remaining:.1f}h remaining)',
        )"""

new_block = """    # COOLDOWN CHECK: skip if this token had a STOP_LOSS within last 24h
    in_cd, hours_remaining = recent_stoploss_cooldown(address)
    if in_cd:
        import time as _time
        return Signal(
            symbol=symbol, address=address, action='HOLD',
            strategy={'A': 'momentum', 'B': 'whale_copy', 'C': 'lottery'}.get(screen, 'momentum'),
            entry_price=0.0, stop_loss=0.0, take_profit=None,
            position_size_usd=0.0,
            reasoning=[f'COOLDOWN: stop-out within {COOLDOWN_HOURS_AFTER_STOPLOSS}h ({hours_remaining:.1f}h remaining)'],
            indicators_snapshot={},
            generated_at=int(_time.time()),
        )"""

if old_block in src:
    src = src.replace(old_block, new_block)
    print("Fixed Signal constructor with all required fields")
else:
    print("WARN: cooldown block not found in expected form")

p.write_text(src, encoding="utf-8")

try:
    ast.parse(src.lstrip("\ufeff"))
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
