# Integration bootstrap — free/read-only setup. Never prints secrets.
# powershell -ExecutionPolicy Bypass -File scripts\bootstrap_integrations.ps1
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $RepoRoot "scripts\rh_live_trader.py"))) {
  $RepoRoot = (Get-Location).Path
}
Set-Location $RepoRoot
Write-Host "REPO=$RepoRoot"

$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
  Write-Host "FAIL: trading-bot .venv python not found at $PythonExe"
  exit 1
}
Write-Host "PYTHON=$PythonExe"
& $PythonExe -c "import sys; print(sys.version)"

$envPath = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envPath)) {
  Write-Host "WARN: .env missing — create it before adding secrets"
} else {
  Write-Host ".env: present (contents not shown)"
}

function Test-Import([string]$mod) {
  & $PythonExe -c "import $mod" 2>$null
  return ($LASTEXITCODE -eq 0)
}

$pkgs = @("httpx","dotenv","pydantic","pytest")
foreach ($m in $pkgs) {
  $ok = Test-Import $m
  Write-Host ("import {0}: {1}" -f $m, ($(if($ok){"OK"}else{"MISSING"})))
  if (-not $ok) {
    if ($m -eq "dotenv") { & $PythonExe -m pip install python-dotenv }
    else { & $PythonExe -m pip install $m }
  }
}
# Optional light Discord lib for future prep
if (-not (Test-Import "discord")) {
  Write-Host "Installing discord.py (gateway not started)"
  & $PythonExe -m pip install discord.py
}
Write-Host "NOTE: FinBERT (transformers/torch) NOT auto-installed here (large). Use doctor FinBERT row."

# Ensure .env.example names (append missing only)
$example = Join-Path $RepoRoot ".env.example"
$needed = @(
  "GOPLUS_ENABLED=false",
  "GOPLUS_APP_KEY=",
  "GOPLUS_APP_SECRET=",
  "X_ENABLED=false",
  "X_BEARER_TOKEN=",
  "X_MAX_REQUESTS_PER_CYCLE=5",
  "X_MAX_DAILY_REQUESTS=50",
  "ETHERSCAN_API_KEY=",
  "CHAINABUSE_API_KEY=",
  "CHAINABUSE_MONTHLY_CALL_CAP=8",
  "DISCORD_ENABLED=false",
  "DISCORD_BOT_TOKEN=",
  "DISCORD_APPLICATION_ID=",
  "DISCORD_GUILD_ID=",
  "DISCORD_OPERATOR_ID=",
  "DISCORD_WEBHOOK_URL=",
  "LUNARCRUSH_ENABLED=false",
  "LUNARCRUSH_API_KEY=",
  "BIRDEYE_API_KEY=",
  "HELIUS_API_KEY=",
  "ZERO_EX_API_KEY="
)
if (-not (Test-Path $example)) { New-Item -ItemType File -Path $example | Out-Null }
$exText = Get-Content $example -Raw -ErrorAction SilentlyContinue
if ($null -eq $exText) { $exText = "" }
$append = @()
foreach ($line in $needed) {
  $name = ($line -split "=",2)[0]
  if ($exText -notmatch "(?m)^$([regex]::Escape($name))=") {
    $append += $line
  }
}
if ($append.Count -gt 0) {
  Add-Content -Path $example -Value ("`r`n# --- integration bootstrap names ---`r`n" + ($append -join "`r`n"))
  Write-Host ("env.example appended {0} names" -f $append.Count)
} else {
  Write-Host "env.example names already present"
}

function Has-EnvName([string]$name) {
  if (-not (Test-Path $envPath)) { return $false }
  $t = Get-Content $envPath -Raw
  # presence of non-empty value without printing
  return [bool]([regex]::Match($t, "(?m)^$([regex]::Escape($name))=\s*\S+").Success)
}

# Open signup pages for missing human credentials
$opens = @()
if (-not ((Has-EnvName "GOPLUS_APP_KEY") -and (Has-EnvName "GOPLUS_APP_SECRET"))) {
  $opens += @{Url="https://www.gopluslabs.io/en/security-api"; Msg="GoPlus FREE App Key/Secret -> GOPLUS_APP_KEY / GOPLUS_APP_SECRET (keep GOPLUS_ENABLED=false)"}
}
if (-not (Has-EnvName "ETHERSCAN_API_KEY")) {
  $opens += @{Url="https://etherscan.io/apis"; Msg="Etherscan FREE key -> ETHERSCAN_API_KEY (do not buy Lite)"}
}
if (-not (Has-EnvName "CHAINABUSE_API_KEY")) {
  $opens += @{Url="https://chainabuse.com/"; Msg="Chainabuse FREE key -> CHAINABUSE_API_KEY (scarce quota)"}
}
if (-not (Has-EnvName "X_BEARER_TOKEN")) {
  $opens += @{Url="https://developer.x.com/"; Msg="Existing X credits -> X_BEARER_TOKEN (keep X_ENABLED=false)"}
}
if (-not (Has-EnvName "DISCORD_BOT_TOKEN")) {
  $opens += @{Url="https://discord.com/developers/applications"; Msg="Discord app/bot -> DISCORD_* vars (DISCORD_ENABLED=false; control paused)"}
}
foreach ($o in $opens) {
  Write-Host ("ACTION REQUIRED: {0}" -f $o.Msg)
  Write-Host ("Opening {0}" -f $o.Url)
  Start-Process $o.Url
}

Write-Host "=== no-key smokes ==="
& $PythonExe scripts\defillama_client.py --smoke-test
& $PythonExe scripts\integration_doctor.py

Write-Host "=== DONE bootstrap (no live enables, no purchases) ==="
