# Windows PowerShell Setup

This guide is for running the trading bot on Windows 10/11 using
PowerShell. Tested with PowerShell 5.1 and PowerShell 7.

## Prerequisites

1. **Python 3.10 or newer.** Install from [python.org](https://www.python.org/downloads/).
   During install, **check "Add Python to PATH"**. Verify:

   ```powershell
   python --version
   ```

   If `python` isn't found, try `py --version`. The launcher works
   even when PATH doesn't.

2. **Git** (optional, for cloning). Install from
   [git-scm.com](https://git-scm.com/download/win) or use the zip
   bundle.

3. **Long path support** (recommended). Some Python deps have deep
   nested paths that exceed Windows' default 260-char limit. Run once
   in an **elevated** PowerShell:

   ```powershell
   New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
       -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
   ```

   Then restart the shell. Skip this step if you don't have admin
   rights — most installs work without it, but a few packages will
   complain.

## One-time setup

Open PowerShell, navigate to wherever you unzipped the project:

```powershell
cd C:\path\to\trading-bot
```

### Allow scripts to run in this session

Windows blocks unsigned scripts by default, including the venv
activator. Run this once per session (or set the policy permanently
for your user):

```powershell
# Just for this session:
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# Or for your user account, permanently:
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

You should see `(.venv)` prepended to your prompt. If activation
fails with an execution policy error, see the step above.

### Install dependencies

```powershell
pip install --upgrade pip
pip install -r requirements.txt
```

This installs ~30 packages and takes a couple of minutes. If
`vectorbt` fails to build, comment it out in `requirements.txt` —
it's optional.

### Configure secrets

```powershell
Copy-Item .env.example .env
notepad .env
```

Fill in:
- `POLYMARKET_PRIVATE_KEY` — hot wallet private key (NEVER your main wallet)
- `POLYMARKET_FUNDER_ADDRESS` — the address holding your USDC
- Leave `LIVE_TRADING_ENABLED=false` until you're ready

The `.env` file is in `.gitignore`. Never commit it.

### Initialize the database

SQLite is the default — no service to start, no Docker. Just:

```powershell
python scripts\init_db.py
```

A `trading_bot.db` file will appear in the project root. To inspect
it, install [DB Browser for SQLite](https://sqlitebrowser.org/) or use:

```powershell
python -c "import sqlite3; c=sqlite3.connect('trading_bot.db'); print([r[0] for r in c.execute('SELECT name FROM sqlite_master WHERE type=\"table\"')])"
```

## Running

### Verify everything works

```powershell
pytest tests\ -v
```

All 34 tests should pass (risk, backtest, score-based, and persistence
suites).

### Smoke test (read-only API connectivity)

```powershell
python scripts\smoke_test.py
```

This pings Polymarket and Solana APIs without placing any orders.
Will warn (not fail) if private keys are blank — fine for an initial
check.

### Scan markets

```powershell
python scripts\scan_markets.py --resolution-window 72 --limit 25
```

### Backtest

```powershell
python scripts\backtest.py --strategy resolution_arb --markets 50 --verbose
```

First run downloads ~50 market histories (a few seconds). Subsequent
runs use the cache in `data\cache\`.

### Streamlit dashboard

```powershell
streamlit run app.py
```

Streamlit opens a browser at `http://localhost:8501`. The dashboard is
read-only by default — it surfaces markets, paper trades, equity, and
runs the backtest inline.

### Paper trading

```powershell
python scripts\run.py --strategies polymarket_resolution_arb --mode paper
```

Paper orders, fills, positions, and equity snapshots are written to
SQLite (`trading_bot.db`) automatically. The dashboard's Paper Trades
and Equity tabs read from that database — refresh to see new trades.

If you also want a JSONL fallback log (legacy):

```powershell
python scripts\run.py --strategies polymarket_resolution_arb --mode paper *> data\paper_log.jsonl
```

PowerShell's `*>` captures all streams (stdout + stderr). The dashboard
falls back to this JSONL file only if the SQLite database is empty.

### Kill switch

If anything goes wrong, in any PowerShell window:

```powershell
python scripts\kill.py
```

Cancels all open orders. Works independently of the main loop.

## Common Windows gotchas

**`pip install` fails with SSL errors** — usually a corporate
proxy / antivirus. Try:

```powershell
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt
```

**`psycopg2` install fails** — you don't need it; we default to
SQLite. Comment that line out in `requirements.txt` if it isn't
already.

**`asyncio` "Event loop is closed" warnings on shutdown** — cosmetic,
known issue with Windows ProactorEventLoop. Doesn't affect trading.

**Activate.ps1 won't run** — execution policy. See the
`Set-ExecutionPolicy` step above.

**Streamlit shows blank page** — try a different browser, or run
`streamlit run app.py --server.headless true` and open the URL
manually.

**`make` doesn't work** — Windows doesn't ship `make`. Use the raw
Python commands shown above, or install
[GnuWin32 make](http://gnuwin32.sourceforge.net/packages/make.htm) /
[chocolatey: `choco install make`].

## Running it as a background service (optional)

For unattended paper trading, use Windows Task Scheduler:

1. Open Task Scheduler
2. Create Task → General: name "trading-bot-paper"
3. Triggers: At log on
4. Actions: Start a program
   - Program: `C:\path\to\trading-bot\.venv\Scripts\python.exe`
   - Arguments: `scripts\run.py --strategies polymarket_resolution_arb --mode paper`
   - Start in: `C:\path\to\trading-bot`
5. Settings: enable "If the task fails, restart every 5 minutes"

Test it manually first by running the action's command in PowerShell.

## What NOT to do on Windows

- **Don't** run as Administrator unless you have a specific reason.
  The bot doesn't need elevated privileges.
- **Don't** put your `.env` in OneDrive or Dropbox sync folders. Cloud
  sync + file locks + private keys = bad combination.
- **Don't** commit `trading_bot.db` to git — it's in `.gitignore` for
  a reason. It contains your full trade history.
- **Don't** run from `C:\Program Files\` or other system paths. Use
  somewhere in your home directory.
