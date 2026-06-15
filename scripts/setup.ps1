<#
.SYNOPSIS
    GigaLib setup script — installs dependencies, configures environment, and verifies the installation.
.DESCRIPTION
    Run this script after cloning the repository to get GigaLib up and running.
    It will:
      1. Check for Python 3.11+ and uv
      2. Install dependencies via uv
      3. Create .env from template (if not exists)
      4. Walk you through API key configuration
      5. Verify platform paths
      6. Run initial sync and enrichment
.EXAMPLE
    .\setup.ps1
#>

$ErrorActionPreference = "Stop"

function Write-Step($num, $msg) {
    Write-Host ""
    Write-Host "[$num] $msg" -ForegroundColor Cyan
    Write-Host ("-" * 60) -ForegroundColor DarkGray
}

function Write-Ok($msg) { Write-Host "  OK: $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  WARN: $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "  ERROR: $msg" -ForegroundColor Red }

# ─────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ╔══════════════════════════════════════╗" -ForegroundColor Magenta
Write-Host "  ║         GigaLib Setup Wizard         ║" -ForegroundColor Magenta
Write-Host "  ╚══════════════════════════════════════╝" -ForegroundColor Magenta
Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────
Write-Step 1 "Checking prerequisites"

# Check Python
$pythonVersion = & python --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Err "Python not found. Install Python 3.11+ from https://python.org"
    exit 1
}
$versionMatch = [regex]::Match($pythonVersion, "(\d+)\.(\d+)")
$major = [int]$versionMatch.Groups[1].Value
$minor = [int]$versionMatch.Groups[2].Value
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
    Write-Err "Python 3.11+ required (found $pythonVersion)"
    exit 1
}
Write-Ok "Python $major.$minor"

# Check uv
$uvPath = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvPath) {
    # Try common install location
    $env:Path = "C:\Users\$env:USERNAME\.local\bin;$env:Path"
    $uvPath = Get-Command uv -ErrorAction SilentlyContinue
}
if (-not $uvPath) {
    Write-Warn "uv not found. Installing..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "C:\Users\$env:USERNAME\.local\bin;$env:Path"
}
$uvVersion = & uv --version 2>&1
Write-Ok "uv $uvVersion"

# ─────────────────────────────────────────────────────────────────────────────
Write-Step 2 "Installing dependencies"

uv sync
if ($LASTEXITCODE -ne 0) {
    Write-Err "uv sync failed"
    exit 1
}
Write-Ok "All packages installed"

# ─────────────────────────────────────────────────────────────────────────────
Write-Step 3 "Configuring environment"

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Ok "Created .env from template"
} else {
    Write-Ok ".env already exists"
}

# Generate SECRET_KEY if still placeholder
$envContent = Get-Content .env -Raw
if ($envContent -match "SECRET_KEY=your-secret-key-here") {
    $secretKey = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ })
    $envContent = $envContent -replace "SECRET_KEY=your-secret-key-here", "SECRET_KEY=$secretKey"
    Set-Content .env $envContent -NoNewline
    Write-Ok "Generated random SECRET_KEY"
}

Write-Host ""
Write-Host "  You'll need API keys for full functionality:" -ForegroundColor White
Write-Host "  ┌─────────────────────────────────────────────────────────────┐" -ForegroundColor DarkGray
Write-Host "  │ STEAM_API_KEY      -> https://steamcommunity.com/dev/apikey │" -ForegroundColor DarkGray
Write-Host "  │ STEAM_USER_ID      -> Your Steam64 ID (steamid.io)         │" -ForegroundColor DarkGray
Write-Host "  │ XBOX_API_KEY       -> https://xbl.io (free)                │" -ForegroundColor DarkGray
Write-Host "  │ TWITCH_CLIENT_ID   -> https://dev.twitch.tv/console        │" -ForegroundColor DarkGray
Write-Host "  │ TWITCH_CLIENT_SECRET -> (same Twitch app)                  │" -ForegroundColor DarkGray
Write-Host "  │ GEMINI_API_KEY     -> https://aistudio.google.com/apikey   │" -ForegroundColor DarkGray
Write-Host "  └─────────────────────────────────────────────────────────────┘" -ForegroundColor DarkGray
Write-Host ""

$configure = Read-Host "  Configure API keys now? (y/n)"
if ($configure -eq "y") {
    $envContent = Get-Content .env -Raw

    function Set-EnvKey($key, $prompt, $url) {
        $current = [regex]::Match($envContent, "$key=(.+)").Groups[1].Value
        if ($current -and $current -notmatch "^your-") {
            Write-Host "    $key already set" -ForegroundColor DarkGray
            return
        }
        Write-Host ""
        Write-Host "    $url" -ForegroundColor Blue
        $value = Read-Host "    Enter $prompt"
        if ($value) {
            $script:envContent = $script:envContent -replace "$key=.*", "$key=$value"
        }
    }

    Set-EnvKey "STEAM_API_KEY" "Steam API Key" "https://steamcommunity.com/dev/apikey"
    Set-EnvKey "STEAM_USER_ID" "Steam User ID (64-bit)" "https://steamid.io"
    Set-EnvKey "XBOX_API_KEY" "OpenXBL API Key" "https://xbl.io"
    Set-EnvKey "TWITCH_CLIENT_ID" "Twitch Client ID" "https://dev.twitch.tv/console"
    Set-EnvKey "TWITCH_CLIENT_SECRET" "Twitch Client Secret" "(same app)"
    Set-EnvKey "GEMINI_API_KEY" "Gemini API Key" "https://aistudio.google.com/apikey"

    Set-Content .env $envContent -NoNewline
    Write-Ok "API keys saved to .env"
}

# ─────────────────────────────────────────────────────────────────────────────
Write-Step 4 "Checking platform paths"

Write-Host "  Verifying paths in platforms.yaml..." -ForegroundColor White
$yaml = Get-Content platforms.yaml -Raw

# Check Steam
$steamPaths = @("C:\Program Files (x86)\Steam", "D:\SteamLibrary")
foreach ($p in $steamPaths) {
    if (Test-Path $p) { Write-Ok "Steam: $p" }
}

# Check EA
if (Test-Path "C:\ProgramData\EA Desktop\InstallData") {
    Write-Ok "EA Desktop: InstallData found"
} else {
    Write-Warn "EA Desktop: InstallData not found (EA Desktop may not be installed)"
}

# Check Ubisoft
$ubiPath = "C:\Program Files (x86)\Ubisoft\Ubisoft Game Launcher"
if (Test-Path $ubiPath) {
    Write-Ok "Ubisoft Connect: $ubiPath"
} else {
    Write-Warn "Ubisoft Connect: not found at default path"
}

Write-Host ""
Write-Host "  Edit platforms.yaml to add/change paths for your system." -ForegroundColor DarkGray

# ─────────────────────────────────────────────────────────────────────────────
Write-Step 5 "Verifying installation"

$result = uv run python -c "from gigalib import create_app; app = create_app(); print('Flask app OK')" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Ok "GigaLib imports correctly"
} else {
    Write-Err "Import failed: $result"
    exit 1
}

# ─────────────────────────────────────────────────────────────────────────────
Write-Step 6 "Done!"

Write-Host ""
Write-Host "  GigaLib is ready to go!" -ForegroundColor Green
Write-Host ""
Write-Host "  Start the dev server:" -ForegroundColor White
Write-Host "    uv run python run.py" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Then open http://127.0.0.1:5000" -ForegroundColor White
Write-Host ""
Write-Host "  First time? Click 'Sync' to detect games, then 'Enrich' to fetch metadata." -ForegroundColor DarkGray
Write-Host ""
