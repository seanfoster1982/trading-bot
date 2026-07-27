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

schtasks /Delete /TN "TradingBotSniper" /F 2>$null
schtasks /Create /TN "TradingBotSniper" /TR "`"$Pyw`" `"$Sniper`"" /SC MINUTE /MO 15 /F
if ($LASTEXITCODE -eq 0) {
    Write-Host "OK  TradingBotSniper - every 15 minutes" -ForegroundColor Green
} else {
    Write-Host "FAIL TradingBotSniper" -ForegroundColor Red
}

schtasks /Delete /TN "TradingBotDigest" /F 2>$null
schtasks /Create /TN "TradingBotDigest" /TR "`"$Pyw`" `"$Digest`" --send" /SC HOURLY /MO 12 /ST 09:00 /F
if ($LASTEXITCODE -eq 0) {
    Write-Host "OK  TradingBotDigest - 9am and 9pm" -ForegroundColor Green
} else {
    Write-Host "FAIL TradingBotDigest" -ForegroundColor Red
}

schtasks /Run /TN "TradingBotWhaleTrace"
Write-Host ""
Write-Host "Whale Trace polling every 15 min. Telegram: shadow opens/closes + digests." -ForegroundColor Yellow
Write-Host "Guide: WHALE_BOT.md" -ForegroundColor Yellow
