# Register Windows scheduled tasks for the Whale Trace product.
# Run once:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_whale_tasks.ps1
#
# Whale Trace is now the primary strategy. The old screener/indicator pipeline
# (TradingBotMonitor) is retired — it only fed the whale_copy strategy, which
# went negative over 31 paper trades.

$Repo = "C:\Users\seanf\Documents\trading-bot"
$Pyw  = "$Repo\.venv\Scripts\pythonw.exe"
$Trace  = "$Repo\scripts\whale_trace.py"
$Sniper = "$Repo\scripts\market_sniper.py"
$Digest = "$Repo\scripts\bot_report.py"
$Live   = "$Repo\scripts\live_trader.py"
$RhLive = "$Repo\scripts\rh_live_trader.py"

Write-Host "=== Whale Trace - task setup ===" -ForegroundColor Cyan

# Retire the old pipeline task if present.
schtasks /Delete /TN "TradingBotMonitor" /F 2>$null

schtasks /Delete /TN "TradingBotWhaleTrace" /F 2>$null
schtasks /Create /TN "TradingBotWhaleTrace" /TR "`"$Pyw`" `"$Trace`"" /SC MINUTE /MO 15 /F
if ($LASTEXITCODE -eq 0) {
    Write-Host "OK  TradingBotWhaleTrace - every 15 minutes" -ForegroundColor Green
} else {
    Write-Host "FAIL TradingBotWhaleTrace" -ForegroundColor Red
}

# Staggered 5 min after whale trace so the two never contend for the DB/API
# at the same clock tick.
schtasks /Delete /TN "TradingBotSniper" /F 2>$null
schtasks /Create /TN "TradingBotSniper" /TR "`"$Pyw`" `"$Sniper`"" /SC MINUTE /MO 15 /ST 00:05 /F
if ($LASTEXITCODE -eq 0) {
    Write-Host "OK  TradingBotSniper - every 15 minutes" -ForegroundColor Green
} else {
    Write-Host "FAIL TradingBotSniper" -ForegroundColor Red
}

# Solana Jupiter live trader stays disabled — capital is on Robinhood Chain.
schtasks /Delete /TN "TradingBotLive" /F 2>$null
schtasks /Create /TN "TradingBotLive" /TR "`"$Pyw`" `"$Live`"" /SC MINUTE /MO 15 /F
if ($LASTEXITCODE -eq 0) {
    schtasks /Change /TN "TradingBotLive" /DISABLE 2>$null
    Write-Host "OK  TradingBotLive - created DISABLED (Solana live off)" -ForegroundColor Green
} else {
    Write-Host "FAIL TradingBotLive" -ForegroundColor Red
}

schtasks /Delete /TN "TradingBotRhLive" /F 2>$null
schtasks /Create /TN "TradingBotRhLive" /TR "`"$Pyw`" `"$RhLive`"" /SC MINUTE /MO 5 /F
if ($LASTEXITCODE -eq 0) {
    Write-Host "OK  TradingBotRhLive - every 5 min (ARMED: $3 micros, 10 slots)" -ForegroundColor Yellow
} else {
    Write-Host "FAIL TradingBotRhLive" -ForegroundColor Red
}

schtasks /Delete /TN "TradingBotRhInbox" /F 2>$null
schtasks /Create /TN "TradingBotRhInbox" /TR "`"$Pyw`" `"$RhLive`" --inbox" /SC MINUTE /MO 1 /F
if ($LASTEXITCODE -eq 0) {
    Write-Host "OK  TradingBotRhInbox - Telegram STATUS/HALT/RESUME every 1 min" -ForegroundColor Green
} else {
    Write-Host "FAIL TradingBotRhInbox" -ForegroundColor Red
}

schtasks /Delete /TN "TradingBotDigest" /F 2>$null
schtasks /Create /TN "TradingBotDigest" /TR "`"$Pyw`" `"$Digest`" --send" /SC HOURLY /MO 12 /ST 09:00 /F
if ($LASTEXITCODE -eq 0) {
    Write-Host "OK  TradingBotDigest - 9am and 9pm" -ForegroundColor Green
} else {
    Write-Host "FAIL TradingBotDigest" -ForegroundColor Red
}

schtasks /Run /TN "TradingBotWhaleTrace"

$Py = "$Repo\.venv\Scripts\python.exe"
Write-Host ""
Write-Host "Checking Telegram phone alerts..." -ForegroundColor Cyan
& $Py "$Repo\scripts\setup_telegram.py"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Telegram is not sending yet. Add TELEGRAM_BOT_TOKEN to .env, message the bot, re-run this script." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Whale Trace polling every 15 min. Telegram: shadow opens/closes + digests." -ForegroundColor Yellow
Write-Host "TradingBotRhLive every 5 min, 10 x $3 micros, Telegram STATUS/HALT/RESUME." -ForegroundColor Yellow
Write-Host "  .venv\Scripts\python.exe scripts\rh_live_trader.py --status" -ForegroundColor Yellow
Write-Host "Guide: WHALE_BOT.md" -ForegroundColor Yellow
