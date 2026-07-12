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

function New-RandomSecret {
    param([int]$Length = 32)

    $chars = ((48..57) + (65..90) + (97..122)) | Get-Random -Count $Length
    return -join ($chars | ForEach-Object { [char]$_ })
}

function Set-EnvValue {
    param(
        [string]$Path,
        [string]$Key,
        [string]$Value
    )

    $content = Get-Content $Path -Raw
    $pattern = "(?m)^$([regex]::Escape($Key))=.*$"
    $replacement = "$Key=$Value"
    if ($content -match $pattern) {
        $content = [regex]::Replace($content, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $replacement })
    } else {
        if (-not $content.EndsWith("`n")) {
            $content += "`n"
        }
        $content += "$replacement`n"
    }

    Set-Content $Path $content -NoNewline
}

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
Write-Ok "Python $($major).$($minor)"

# Check uv
$uvPath = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvPath) {
    # Try common install location
    $userLocalBin = Join-Path ('C:\Users\' + $env:USERNAME) '.local\bin'
    $env:Path = $userLocalBin + ';' + $env:Path
    $uvPath = Get-Command uv -ErrorAction SilentlyContinue
}
if (-not $uvPath) {
    Write-Warn 'uv not found. Installing...'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $userLocalBin = Join-Path ('C:\Users\' + $env:USERNAME) '.local\bin'
    $env:Path = $userLocalBin + ';' + $env:Path
}
$uvVersion = & uv --version 2>&1
Write-Ok ("uv " + $uvVersion)

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
    $secretKey = New-RandomSecret
    Set-EnvValue -Path ".env" -Key "SECRET_KEY" -Value $secretKey
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

    Set-EnvKey -key "STEAM_API_KEY" -prompt "Steam API Key" -url "https://steamcommunity.com/dev/apikey"
    Set-EnvKey -key "STEAM_USER_ID" -prompt "Steam User ID 64-bit" -url "https://steamid.io"
    Set-EnvKey -key "XBOX_API_KEY" -prompt "OpenXBL API Key" -url "https://xbl.io"
    Set-EnvKey -key "TWITCH_CLIENT_ID" -prompt "Twitch Client ID" -url "https://dev.twitch.tv/console"
    Set-EnvKey -key "TWITCH_CLIENT_SECRET" -prompt "Twitch Client Secret" -url "(same app)"
    Set-EnvKey -key "GEMINI_API_KEY" -prompt "Gemini API Key" -url "https://aistudio.google.com/apikey"

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
Write-Step 6 "Installing voice models (Kokoro TTS + Whisper STT)"

Write-Host "  GigaLib's AI companion can speak (Kokoro TTS) and listen"      -ForegroundColor White
Write-Host "  (faster-whisper STT), both running fully locally — no cloud,"  -ForegroundColor White
Write-Host "  no quotas. Total download is about 480 MB and is idempotent"   -ForegroundColor White
Write-Host "  (skips files that are already present)."                       -ForegroundColor White
Write-Host ""
Write-Host "    Kokoro TTS model  ~ 337 MB  -> instance\kokoro\"             -ForegroundColor DarkGray
Write-Host "    Whisper STT model ~ 140 MB  -> instance\whisper\ (base)"     -ForegroundColor DarkGray
Write-Host ""

$instanceDir = Join-Path (Get-Location) "instance"
$kokoroDir   = Join-Path $instanceDir "kokoro"
$whisperDir  = Join-Path $instanceDir "whisper"
New-Item -ItemType Directory -Force -Path $kokoroDir  | Out-Null
New-Item -ItemType Directory -Force -Path $whisperDir | Out-Null

$kokoroFiles = @(
    @{ Name = "kokoro-v1.0.onnx"; Url = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx" },
    @{ Name = "voices-v1.0.bin"; Url = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin" }
)
foreach ($f in $kokoroFiles) {
    $dest = Join-Path $kokoroDir $f.Name
    if (Test-Path $dest) {
        Write-Ok "Kokoro: $($f.Name) already present"
        continue
    }
    Write-Host "    Downloading $($f.Name)..." -ForegroundColor DarkGray
    try {
        # Invoke-WebRequest streams to disk with a progress bar. Basic
        # parsing avoids the legacy IE COM engine.
        Invoke-WebRequest -Uri $f.Url -OutFile $dest -UseBasicParsing
        Write-Ok "Kokoro: $($f.Name) downloaded"
    } catch {
        Write-Err "Kokoro download failed for $($f.Name): $_"
        if (Test-Path $dest) { Remove-Item $dest -Force -ErrorAction SilentlyContinue }
    }
}

Write-Host "    Warming faster-whisper (base) — first run downloads the model..." -ForegroundColor DarkGray
$whisperWarm = @"
import os, sys
os.environ.setdefault('WHISPER_CACHE_DIR', r'$whisperDir')
try:
    from faster_whisper import WhisperModel
    WhisperModel('base', device='cpu', compute_type='int8', download_root=r'$whisperDir')
    print('whisper OK')
except Exception as exc:
    print('whisper FAIL:', exc, file=sys.stderr)
    sys.exit(1)
"@
$whisperResult = $whisperWarm | uv run python - 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Ok "Whisper: base model ready"
} else {
    Write-Err "Whisper warm-up failed: $whisperResult"
}

# ─────────────────────────────────────────────────────────────────────────────
Write-Step 7 "Installing app startup task"

$installTasks = Read-Host "  Install Windows startup task for GigaLib? (y/n)"
if ($installTasks -eq "y") {
    uv run python scripts/install_service.py install --target app
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Startup task installation failed"
        exit 1
    }
    Write-Ok "Startup task installed"
}

# ─────────────────────────────────────────────────────────────────────────────
Write-Step 8 "Done!"

Write-Host ""
Write-Host "  GigaLib is ready to go!" -ForegroundColor Green
Write-Host ""
Write-Host "  Start the dev server:" -ForegroundColor White
Write-Host "    uv run python run.py" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Then open http://127.0.0.1:5000" -ForegroundColor White
Write-Host ""
Write-Host "  First time? Click 'Sync' to detect games, then 'Enrich' to fetch metadata." -ForegroundColor DarkGray
Write-Host "  If you installed the startup task, GigaLib will auto-launch at login." -ForegroundColor DarkGray
Write-Host "  Run scripts/install_social.ps1 separately if you want the Social API installed too." -ForegroundColor DarkGray
Write-Host ""
