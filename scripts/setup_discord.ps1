<#
.SYNOPSIS
    Discord Operations Console Setup Wizard for Trading Bot (Phase 2D).

.DESCRIPTION
    Interactive setup:
    1. Detect repo + .venv
    2. pip install discord.py if missing using venv python
    3. Verify .env presence
    4. Open Discord developer applications URL
    5. Prompt locally for token (no echo), app id, guild id, operator user id
    6. Write to local .env; keep DISCORD_ENABLED=false until verify
    7. Generate OAuth invite URL with MINIMUM permissions
    8. Open invite URL
    9. After bot in guild, continue to bootstrap channels
    10. Never print token

.NOTES
    Run with: powershell -ExecutionPolicy Bypass -File scripts/setup_discord.ps1
#>

$ErrorActionPreference = "Stop"

function Write-Info { param([string]$Message) Write-Host "[INFO] $Message" -ForegroundColor Cyan }
function Write-Success { param([string]$Message) Write-Host "[OK] $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "[WARN] $Message" -ForegroundColor Yellow }
function Write-Err { param([string]$Message) Write-Host "[ERROR] $Message" -ForegroundColor Red }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$EnvPath = Join-Path $RepoRoot ".env"
$VenvPath = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

Write-Host ""
Write-Host "========================================" -ForegroundColor Magenta
Write-Host "  DISCORD OPERATIONS CONSOLE SETUP     " -ForegroundColor Magenta
Write-Host "  Trading Bot Phase 2D                 " -ForegroundColor Magenta
Write-Host "========================================" -ForegroundColor Magenta
Write-Host ""

# 1. Detect repo
if (-not (Test-Path (Join-Path $RepoRoot "scripts\stop_control.py"))) {
    Write-Err "Not in trading-bot repo. Run from repo root: powershell -File scripts/setup_discord.ps1"
    exit 1
}
Write-Success "Repo detected: $RepoRoot"

# 2. Detect .venv
if (-not (Test-Path $VenvPython)) {
    Write-Warn ".venv not found at $VenvPath"
    Write-Info "Creating virtual environment..."
    python -m venv $VenvPath
    if (-not (Test-Path $VenvPython)) {
        Write-Err "Failed to create .venv"
        exit 1
    }
}
Write-Success "Virtual environment: $VenvPath"

# 3. pip install discord.py if missing
Write-Info "Checking discord.py installation..."
$discordCheck = & $VenvPython -c "import discord; print(discord.__version__)" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Info "Installing discord.py..."
    & $VenvPython -m pip install "discord.py>=2.3.0" --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed to install discord.py"
        exit 1
    }
    $discordCheck = & $VenvPython -c "import discord; print(discord.__version__)" 2>&1
}
Write-Success "discord.py version: $discordCheck"

# 4. Verify .env exists
if (-not (Test-Path $EnvPath)) {
    Write-Info "Creating .env from .env.example..."
    Copy-Item (Join-Path $RepoRoot ".env.example") $EnvPath
}
Write-Success ".env found: $EnvPath"

# Read current .env into hashtable
function Read-EnvFile {
    param([string]$Path)
    $env = @{}
    if (Test-Path $Path) {
        Get-Content $Path | ForEach-Object {
            if ($_ -match "^\s*([^#][^=]+)=(.*)$") {
                $key = $Matches[1].Trim()
                $val = $Matches[2].Trim().Trim('"').Trim("'")
                $env[$key] = $val
            }
        }
    }
    return $env
}

function Write-EnvFile {
    param([string]$Path, [hashtable]$Env)
    $existingContent = if (Test-Path $Path) { Get-Content $Path -Raw } else { "" }
    foreach ($key in $Env.Keys) {
        $val = $Env[$key]
        $pattern = "(?m)^(\s*)$key\s*=.*$"
        if ($existingContent -match $pattern) {
            $existingContent = $existingContent -replace $pattern, "`$1$key=$val"
        } else {
            $existingContent += "`n$key=$val"
        }
    }
    Set-Content -Path $Path -Value $existingContent.TrimEnd() -NoNewline
    Add-Content -Path $Path -Value ""
}

$currentEnv = Read-EnvFile $EnvPath

# Check if already configured
$hasToken = -not [string]::IsNullOrWhiteSpace($currentEnv["DISCORD_BOT_TOKEN"])
$hasAppId = -not [string]::IsNullOrWhiteSpace($currentEnv["DISCORD_APPLICATION_ID"])
$hasGuildId = -not [string]::IsNullOrWhiteSpace($currentEnv["DISCORD_GUILD_ID"])
$hasOperatorId = -not [string]::IsNullOrWhiteSpace($currentEnv["DISCORD_OPERATOR_ID"])

if ($hasToken -and $hasAppId -and $hasGuildId -and $hasOperatorId) {
    Write-Success "Discord already configured in .env"
    $reconfig = Read-Host "Reconfigure? (y/N)"
    if ($reconfig -ne "y" -and $reconfig -ne "Y") {
        Write-Info "Skipping credential prompts. Proceeding to verify..."
        $skipPrompts = $true
    }
}

if (-not $skipPrompts) {
    # 5. Open Discord developer portal
    Write-Host ""
    Write-Info "Opening Discord Developer Portal..."
    Write-Host "  1. Click 'New Application' or select your existing bot application"
    Write-Host "  2. Go to 'Bot' in the left sidebar"
    Write-Host "  3. Click 'Reset Token' and copy the new token"
    Write-Host "  4. Enable 'Message Content Intent' under Privileged Gateway Intents"
    Write-Host ""
    Start-Process "https://discord.com/developers/applications"
    Start-Sleep -Seconds 2
    Read-Host "Press Enter when you have your bot token ready..."

    # 6. Prompt for credentials (no echo for token)
    Write-Host ""
    Write-Host "Enter your Discord credentials (token will be hidden):" -ForegroundColor Yellow

    # Token - use SecureString for no echo
    $secureToken = Read-Host "Bot Token" -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
    $botToken = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    
    if ([string]::IsNullOrWhiteSpace($botToken)) {
        Write-Err "Bot token is required"
        exit 1
    }

    # Application ID
    $appId = Read-Host "Application ID (from General Information page)"
    if ([string]::IsNullOrWhiteSpace($appId) -or $appId -notmatch "^\d+$") {
        Write-Err "Valid Application ID required (numeric)"
        exit 1
    }

    # Guild ID
    Write-Host ""
    Write-Host "To get your Guild (Server) ID:" -ForegroundColor Yellow
    Write-Host "  1. Enable Developer Mode: User Settings > App Settings > Advanced > Developer Mode"
    Write-Host "  2. Right-click your server icon and select 'Copy Server ID'"
    Write-Host ""
    $guildId = Read-Host "Guild (Server) ID"
    if ([string]::IsNullOrWhiteSpace($guildId) -or $guildId -notmatch "^\d+$") {
        Write-Err "Valid Guild ID required (numeric)"
        exit 1
    }

    # Operator User ID
    Write-Host ""
    Write-Host "To get your User ID:" -ForegroundColor Yellow
    Write-Host "  Right-click your username anywhere in Discord and select 'Copy User ID'"
    Write-Host ""
    $operatorId = Read-Host "Your User ID (operator who can HALT/RESUME)"
    if ([string]::IsNullOrWhiteSpace($operatorId) -or $operatorId -notmatch "^\d+$") {
        Write-Err "Valid User ID required (numeric)"
        exit 1
    }

    # 7. Write to .env (DISCORD_ENABLED stays false)
    $updates = @{
        "DISCORD_BOT_TOKEN" = $botToken
        "DISCORD_APPLICATION_ID" = $appId
        "DISCORD_GUILD_ID" = $guildId
        "DISCORD_OPERATOR_ID" = $operatorId
        "DISCORD_ENABLED" = "false"
    }
    Write-EnvFile $EnvPath $updates
    Write-Success "Credentials saved to .env (DISCORD_ENABLED=false until verified)"
    Write-Host "  Token saved: YES (not displayed for security)" -ForegroundColor DarkGray

    # Reload env
    $currentEnv = Read-EnvFile $EnvPath
    $appId = $currentEnv["DISCORD_APPLICATION_ID"]
}

# 8. Generate and open OAuth invite URL
# MINIMUM permissions: View Channels, Send Messages, Embed Links, Read Message History, Use Application Commands
# Temporarily Manage Channels for bootstrap (not Administrator!)
# Permission calculator: 
#   VIEW_CHANNEL = 0x400 (1024)
#   SEND_MESSAGES = 0x800 (2048)
#   EMBED_LINKS = 0x4000 (16384)
#   READ_MESSAGE_HISTORY = 0x10000 (65536)
#   USE_APPLICATION_COMMANDS = 0x80000000 (2147483648)
#   MANAGE_CHANNELS = 0x10 (16) - for bootstrap only
# Total: 1024 + 2048 + 16384 + 65536 + 2147483648 + 16 = 2147568656

$appId = $currentEnv["DISCORD_APPLICATION_ID"]
$permissions = 2147568656
$inviteUrl = "https://discord.com/api/oauth2/authorize?client_id=$appId&permissions=$permissions&scope=bot%20applications.commands"

Write-Host ""
Write-Host "========================================" -ForegroundColor Yellow
Write-Host "  INVITE BOT TO YOUR SERVER            " -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Yellow
Write-Host ""
Write-Host "Invite URL (opening in browser):" -ForegroundColor Cyan
Write-Host $inviteUrl
Write-Host ""
Write-Host "Permissions requested (MINIMUM):" -ForegroundColor DarkGray
Write-Host "  - View Channels" -ForegroundColor DarkGray
Write-Host "  - Send Messages" -ForegroundColor DarkGray
Write-Host "  - Embed Links" -ForegroundColor DarkGray
Write-Host "  - Read Message History" -ForegroundColor DarkGray
Write-Host "  - Use Application Commands" -ForegroundColor DarkGray
Write-Host "  - Manage Channels (for bootstrap only)" -ForegroundColor DarkGray
Write-Host ""

Start-Process $inviteUrl
Read-Host "Press Enter after adding the bot to your server..."

# 9. Verify connection and run bootstrap
Write-Host ""
Write-Info "Verifying bot connection..."

$verifyScript = @"
import sys
sys.path.insert(0, '$($RepoRoot.Replace('\', '\\'))\\scripts')
from dotenv import load_dotenv
load_dotenv('$($EnvPath.Replace('\', '\\'))')
import os
import asyncio
import discord

async def verify():
    token = os.getenv('DISCORD_BOT_TOKEN', '').strip()
    guild_id = os.getenv('DISCORD_GUILD_ID', '').strip()
    if not token or not guild_id:
        print('MISSING_CREDS')
        return 1
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    
    @client.event
    async def on_ready():
        guild = client.get_guild(int(guild_id))
        if guild:
            print(f'CONNECTED:{client.user.name}:{guild.name}')
        else:
            print(f'BOT_OK_NO_GUILD:{client.user.name}')
        await client.close()
    
    try:
        await asyncio.wait_for(client.start(token), timeout=30.0)
    except asyncio.TimeoutError:
        print('TIMEOUT')
        return 1
    except discord.LoginFailure:
        print('AUTH_FAILED')
        return 1
    except Exception as e:
        print(f'ERROR:{type(e).__name__}')
        return 1
    return 0

sys.exit(asyncio.run(verify()))
"@

$result = & $VenvPython -c $verifyScript 2>&1
$exitCode = $LASTEXITCODE

if ($result -match "^CONNECTED:(.+):(.+)$") {
    $botName = $Matches[1]
    $guildName = $Matches[2]
    Write-Success "Bot connected as: $botName"
    Write-Success "Guild found: $guildName"
} elseif ($result -match "^BOT_OK_NO_GUILD:") {
    Write-Warn "Bot authenticated but cannot see the guild. Check permissions."
    exit 1
} elseif ($result -eq "AUTH_FAILED") {
    Write-Err "Bot token rejected by Discord. Check the token."
    exit 1
} elseif ($result -eq "TIMEOUT") {
    Write-Err "Connection timed out."
    exit 1
} else {
    Write-Err "Verification failed: $result"
    exit 1
}

# 10. Run bootstrap_discord.py to create channels
Write-Host ""
Write-Info "Running channel bootstrap..."
& $VenvPython (Join-Path $RepoRoot "scripts\bootstrap_discord.py") --bootstrap
$bootstrapCode = $LASTEXITCODE

if ($bootstrapCode -eq 0) {
    Write-Success "Channel bootstrap complete"
} else {
    Write-Warn "Channel bootstrap had issues (exit code $bootstrapCode)"
    Write-Host "You may need to manually create missing channels or check bot permissions."
}

# Final summary
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  SETUP COMPLETE                       " -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Verify channels were created in your server under 'TRADING BOT OPS' category"
Write-Host "  2. Test the gateway: .venv\Scripts\python scripts\discord_gateway.py --connect-test"
Write-Host "  3. When ready, set DISCORD_ENABLED=true in .env"
Write-Host "  4. Start the persistent gateway: .venv\Scripts\python scripts\discord_gateway.py"
Write-Host "  5. Or install as Windows Task: powershell -File scripts\setup_discord_task.ps1"
Write-Host ""
Write-Host "WARNING: Do not share your bot token. It is stored in .env (gitignored)." -ForegroundColor Red
Write-Host ""
