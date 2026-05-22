from pathlib import Path
import ast

p = Path("scripts/backtest.py")
src = p.read_text(encoding="utf-8")

# Add a verbose diagnostic function that traces why a single token had no trades
old_main = """def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(\'--days\', type=int, default=30)
    parser.add_argument(\'--max-tokens\', type=int, default=0)
    parser.add_argument(\'--start-capital\', type=float, default=100.0)
    args = parser.parse_args()
    run_backtest(args.days, args.max_tokens or 999, args.start_capital)"""

new_main = """def diagnose_token(symbol: str, days: int):
    \\\"\\\"\\\"Walk through one token verbose - report why each candle didn\\'t fire.\\\"\\\"\\\"
    since_ts = int(time.time()) - days * 86400
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        \'SELECT address FROM screened_tokens WHERE symbol = ? LIMIT 1\',
        (symbol,)
    ).fetchone()
    conn.close()
    if not row:
        print(f\'Token {symbol} not in screened_tokens\')
        return
    address = row[0]
    print(f\'Diagnosing {symbol} ({address[:8]}...) over {days} days\')

    df_5m = load_candles(address, \'5m\', since_ts)
    df_15m = load_candles(address, \'15m\', since_ts)
    df_1h = load_candles(address, \'1H\', since_ts)
    df_4h = load_candles(address, \'4H\', since_ts)
    df_1d = load_candles(address, \'1D\', since_ts)

    print(f\'  Candles loaded: 5m={len(df_5m)} 15m={len(df_15m)} 1H={len(df_1h)} 4H={len(df_4h)} 1D={len(df_1d)}\')

    if len(df_5m) < 300:
        print(f\'  Skipped: not enough 5m candles (need 300, have {len(df_5m)})\')
        return

    df_5m = compute_indicators_inline(df_5m)
    df_15m = compute_indicators_inline(df_15m)
    df_1h = compute_indicators_inline(df_1h)
    df_4h = compute_indicators_inline(df_4h)
    df_1d = compute_indicators_inline(df_1d)

    # Tally reasons for rejection
    reasons = defaultdict(int)
    samples = []

    for i in range(200, len(df_5m) - 1):
        row_5m = df_5m.iloc[i]
        ts = int(row_5m[\'timestamp\'])

        idx_1d = find_htf_index(df_1d, ts)
        if idx_1d < 0:
            reasons[\'1d_no_htf_candle\'] += 1
            continue
        if not htf_aligned_bullish(df_1d, idx_1d):
            reasons[\'1d_not_bullish\'] += 1
            continue
        idx_4h = find_htf_index(df_4h, ts)
        if idx_4h < 0 or not htf_aligned_bullish(df_4h, idx_4h):
            reasons[\'4h_not_bullish\'] += 1
            continue
        idx_1h = find_htf_index(df_1h, ts)
        if idx_1h < 0 or not htf_aligned_bullish(df_1h, idx_1h):
            reasons[\'1h_not_bullish\'] += 1
            continue
        idx_15m = find_htf_index(df_15m, ts)
        if idx_15m < 1:
            reasons[\'15m_no_data\'] += 1
            continue
        trigger = check_15m_entry_trigger(df_15m.iloc[idx_15m], df_15m.iloc[idx_15m - 1])
        if trigger is None:
            reasons[\'15m_no_trigger\'] += 1
            continue
        if not above_vwap_5m(row_5m):
            reasons[\'5m_below_vwap\'] += 1
            continue
        atr = row_5m.get(\'atr\')
        if pd.isna(atr) or atr <= 0:
            reasons[\'no_atr\'] += 1
            continue
        reasons[\'WOULD_HAVE_FIRED\'] += 1
        if len(samples) < 5:
            samples.append((ts, trigger, float(row_5m[\'close\']), float(atr)))

    print(f\'  Total 5m candles evaluated: {len(df_5m) - 200}\')
    print(f\'  Rejection reasons (most to least):\')
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        pct = count / (len(df_5m) - 200) * 100
        print(f\'    {reason}: {count} ({pct:.1f}%)\')

    if samples:
        print(f\'  Sample setups that would have fired:\')
        for ts, trig, price, atr in samples:
            dt = datetime.fromtimestamp(ts, timezone.utc).strftime(\'%Y-%m-%d %H:%M UTC\')
            print(f\'    {dt}  {trig}  price={price:.8f}  atr={atr:.8f}\')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(\'--days\', type=int, default=30)
    parser.add_argument(\'--max-tokens\', type=int, default=0)
    parser.add_argument(\'--start-capital\', type=float, default=100.0)
    parser.add_argument(\'--diagnose\', type=str, default=None,
                        help=\'Diagnose a single token by symbol (e.g. --diagnose BULL)\')
    args = parser.parse_args()
    if args.diagnose:
        diagnose_token(args.diagnose, args.days)
    else:
        run_backtest(args.days, args.max_tokens or 999, args.start_capital)"""

if old_main in src:
    src = src.replace(old_main, new_main)
    print("Added diagnose mode")
else:
    print("WARN: main function not found in expected form")

p.write_text(src, encoding="utf-8")

try:
    ast.parse(src)
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")
