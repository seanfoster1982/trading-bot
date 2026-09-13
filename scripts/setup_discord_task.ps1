<#
.SYNOPSIS
    Windows Task Scheduler setup for Discord Gateway (Trading Bot Phase 2D).

.DESCRIPTION
    Creates Windows Scheduled Task "TradingBotDiscord" that:
    - Uses .venv pythonw.exe to run discord_gateway.py
    - WorkingDirectory = repo root
    - Starts at logon
    - ONE persistent instance (no duplicates)
    - NOT every 5 minutes (persistent bot, not periodic)

.NOTES
    Run with: powershell -ExecutionPolicy Bypass -File scripts/setup_discord_task.ps1
    Requires Administrator for task creation.
#>

$ErrorActionPreference = "Stop"

function Write-Info { param([string]$Message) Write-Host "[INFO] $Message" -ForegroundColor Cyan }
function Write-Success { param([string]$Message) Write-Host "[OK] $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "[WARN] $Message" -ForegroundColor Yellow }
function Write-Err { param([string]$Message) Write-Host "[ERROR] $Message" -ForegroundColor Red }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$TaskName = "TradingBotDiscord"

$VenvPythonW = Join-Path $RepoRoot ".venv\Scripts\pythonw.exe"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$GatewayScript = Join-Path $RepoRoot "scripts\discord_gateway.py"

Write-Host ""
Write-Host "========================================" -ForegroundColor Magenta
Write-Host "  DISCORD TASK SCHEDULER SETUP         " -ForegroundColor Magenta
Write-Host "  Trading Bot Phase 2D                 " -ForegroundColor Magenta
Write-Host "========================================" -ForegroundColor Magenta
Write-Host ""

# Check admin
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warn "Not running as Administrator. Task creation may fail."
    Write-Host "Recommend: Run PowerShell as Administrator" -ForegroundColor Yellow
}

# Verify paths
if (-not (Test-Path $VenvPythonW)) {
    if (Test-Path $VenvPython) {
        Write-Warn "pythonw.exe not found, using python.exe (will show console window)"
        $VenvPythonW = $VenvPython
    } else {
        Write-Err ".venv not found at $RepoRoot\.venv"
        Write-Host "Run: python -m venv .venv"
        exit 1
    }
}

if (-not (Test-Path $GatewayScript)) {
    Write-Err "discord_gateway.py not found at $GatewayScript"
    exit 1
}

Write-Success "Paths verified"
Write-Host "  Python: $VenvPythonW" -ForegroundColor DarkGray
Write-Host "  Script: $GatewayScript" -ForegroundColor DarkGray
Write-Host "  WorkDir: $RepoRoot" -ForegroundColor DarkGray

# Check for existing task
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($existingTask) {
    Write-Warn "Task '$TaskName' already exists"
    $choice = Read-Host "Replace existing task? (y/N)"
    if ($choice -eq "y" -or $choice -eq "Y") {
        Write-Info "Removing existing task..."
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    } else {
        Write-Host "Keeping existing task. Exiting."
        exit 0
    }
}

# Create task
Write-Info "Creating scheduled task..."

$action = New-ScheduledTaskAction `
    -Execute $VenvPythonW `
    -Argument "`"$GatewayScript`"" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "Discord Operations Console for Trading Bot. Persistent gateway - starts at logon, single instance only." `
        -Force | Out-Null
    
    Write-Success "Task '$TaskName' created successfully"
} catch {
    Write-Err "Failed to create task: $_"
    Write-Host ""
    Write-Host "Manual alternative:" -ForegroundColor Yellow
    Write-Host "  1. Open Task Scheduler (taskschd.msc)"
    Write-Host "  2. Create Basic Task named: $TaskName"
    Write-Host "  3. Trigger: At log on"
    Write-Host "  4. Action: Start a program"
    Write-Host "     Program: $VenvPythonW"
    Write-Host "     Arguments: `"$GatewayScript`""
    Write-Host "     Start in: $RepoRoot"
    Write-Host "  5. In Properties: Allow multiple instances = Do not start new instance"
    exit 1
}

# Show task info
Write-Host ""
Write-Host "Task Configuration:" -ForegroundColor Cyan
Write-Host "  Name: $TaskName"
Write-Host "  Trigger: At logon"
Write-Host "  Action: $VenvPythonW `"$GatewayScript`""
Write-Host "  Working Directory: $RepoRoot"
Write-Host "  Multiple Instances: IgnoreNew (single instance only)"

# Offer to start now
Write-Host ""
$startNow = Read-Host "Start the task now? (Y/n)"
if ($startNow -ne "n" -and $startNow -ne "N") {
    Write-Info "Starting task..."
    try {
        Start-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 2
        $taskInfo = Get-ScheduledTask -TaskName $TaskName
        Write-Host "  State: $($taskInfo.State)"
        if ($taskInfo.State -eq "Running") {
            Write-Success "Discord gateway is running"
        } else {
            Write-Warn "Task started but may have exited. Check DISCORD_ENABLED in .env"
        }
    } catch {
        Write-Warn "Could not start task: $_"
    }
}

# Summary
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  SETUP COMPLETE                       " -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Commands:" -ForegroundColor Yellow
Write-Host "  Check status:  Get-ScheduledTask -TaskName $TaskName"
Write-Host "  Start:         Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Stop:          Stop-ScheduledTask -TaskName $TaskName"
Write-Host "  Remove:        Unregister-ScheduledTask -TaskName $TaskName"
Write-Host ""
Write-Host "Logs: $RepoRoot\logs\discord_gateway.log"
Write-Host ""
