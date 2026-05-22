from pathlib import Path
import ast

p = Path("scripts/backtest.py")
src = p.read_text(encoding="utf-8")

# Strip the escape backslashes from the docstring and any string inside
# the diagnose_token function. They came from the patch's escape handling.
replacements = [
    ('\\"\\"\\"', '"""'),
    ("didn\\'t", "didnt"),
    ("\\'5m\\'", "'5m'"),
    ("\\'15m\\'", "'15m'"),
    ("\\'1H\\'", "'1H'"),
    ("\\'4H\\'", "'4H'"),
    ("\\'1D\\'", "'1D'"),
    ("\\'symbol\\'", "'symbol'"),
    ("\\'address\\'", "'address'"),
    ("\\'timestamp\\'", "'timestamp'"),
    ("\\'close\\'", "'close'"),
    ("\\'atr\\'", "'atr'"),
    ("\\'1d_no_htf_candle\\'", "'1d_no_htf_candle'"),
    ("\\'1d_not_bullish\\'", "'1d_not_bullish'"),
    ("\\'4h_not_bullish\\'", "'4h_not_bullish'"),
    ("\\'1h_not_bullish\\'", "'1h_not_bullish'"),
    ("\\'15m_no_data\\'", "'15m_no_data'"),
    ("\\'15m_no_trigger\\'", "'15m_no_trigger'"),
    ("\\'5m_below_vwap\\'", "'5m_below_vwap'"),
    ("\\'no_atr\\'", "'no_atr'"),
    ("\\'WOULD_HAVE_FIRED\\'", "'WOULD_HAVE_FIRED'"),
    ("\\'%Y-%m-%d %H:%M UTC\\'", "'%Y-%m-%d %H:%M UTC'"),
    ("\\'SELECT address FROM screened_tokens WHERE symbol = ? LIMIT 1\\'",
     "'SELECT address FROM screened_tokens WHERE symbol = ? LIMIT 1'"),
    ("\\'--days\\'", "'--days'"),
    ("\\'--max-tokens\\'", "'--max-tokens'"),
    ("\\'--start-capital\\'", "'--start-capital'"),
    ("\\'--diagnose\\'", "'--diagnose'"),
    ("\\'Diagnose a single token by symbol (e.g. --diagnose BULL)\\'",
     "'Diagnose a single token by symbol (e.g. --diagnose BULL)'"),
]

total = 0
for old, new in replacements:
    n = src.count(old)
    if n:
        src = src.replace(old, new)
        total += n

p.write_text(src, encoding="utf-8")
print(f"Replaced {total} escaped sequences")

try:
    ast.parse(src)
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error still present: {e}")
    print(f"Look at line {e.lineno}: {e.text!r}" if e.text else "")
