# morning_check.ps1 - run all status checks in one go
# Usage: .\morning_check.ps1
# Note: skips execution_coordinator (interactive) - run separately when ready to trade

$ErrorActionPreference = "Continue"
$startTime = Get-Date

Write-Host ""
Write-Host "=============================================================" -ForegroundColor Cyan
Write-Host " MORNING CHECK - $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
Write-Host "=============================================================" -ForegroundColor Cyan

Write-Host ""
Write-Host "[1/4] Wallet balances..." -ForegroundColor Yellow
Write-Host "-------------------------------------------------------------" -ForegroundColor DarkGray
python scripts\balance_reader.py

Write-Host ""
Write-Host "[2/4] Paper trader positions..." -ForegroundColor Yellow
Write-Host "-------------------------------------------------------------" -ForegroundColor DarkGray
python scripts\paper_trader.py

Write-Host ""
Write-Host "[3/4] Daily report (last 24h)..." -ForegroundColor Yellow
Write-Host "-------------------------------------------------------------" -ForegroundColor DarkGray
python scripts\daily_report.py --hours 24

Write-Host ""
Write-Host "[4/4] Go-live readiness check..." -ForegroundColor Yellow
Write-Host "-------------------------------------------------------------" -ForegroundColor DarkGray
python scripts\go_live_dashboard.py

$elapsed = (Get-Date) - $startTime
Write-Host ""
Write-Host "=============================================================" -ForegroundColor Cyan
Write-Host " DONE in $([math]::Round($elapsed.TotalSeconds, 1))s" -ForegroundColor Cyan
Write-Host "=============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "If you want to execute pending live trades, run separately:" -ForegroundColor DarkGray
Write-Host "  python scripts\execution_coordinator.py" -ForegroundColor DarkGray
Write-Host ""
