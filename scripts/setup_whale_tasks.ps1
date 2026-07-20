# Register Windows scheduled tasks for the Whale Copy Bot product.
# Run once:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_whale_tasks.ps1

$Repo = "C:\Users\seanf\Documents\trading-bot"
$Pyw  = "$Repo\.venv\Scripts\pythonw.exe"
$Monitor = "$Repo\scripts\monitor_loop.py"
$Digest  = "$Repo\scripts\bot_report.py"

Write-Host "=== Whale Copy Bot - task setup ===" -ForegroundColor Cyan

schtasks /Delete /TN "TradingBotMonitor" /F 2>$null
schtasks /Create /TN "TradingBotMonitor" /TR "`"$Pyw`" `"$Monitor`" --once" /SC MINUTE /MO 60 /F
if ($LASTEXITCODE -eq 0) {
    Write-Host "OK  TradingBotMonitor - every 60 minutes" -ForegroundColor Green
} else {
    Write-Host "FAIL TradingBotMonitor" -ForegroundColor Red
}

schtasks /Delete /TN "TradingBotDigest" /F 2>$null
schtasks /Create /TN "TradingBotDigest" /TR "`"$Pyw`" `"$Digest`" --send" /SC HOURLY /MO 12 /ST 09:00 /F
if ($LASTEXITCODE -eq 0) {
    Write-Host "OK  TradingBotDigest - 9am and 9pm" -ForegroundColor Green
} else {
    Write-Host "FAIL TradingBotDigest" -ForegroundColor Red
}

schtasks /Change /TN "TradingBotMonitor" /ENABLE
schtasks /Run /TN "TradingBotMonitor"
Write-Host ""
Write-Host "First pipeline cycle started. Check data/overnight.log in ~6 min." -ForegroundColor Yellow
Write-Host "Telegram: BUY/close alerts + 9am/9pm digest." -ForegroundColor Yellow
Write-Host "Guide: WHALE_BOT.md" -ForegroundColor Yellow
