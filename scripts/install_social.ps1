<#
.SYNOPSIS
    Installs the standalone GigaLib Social API on Windows.
.DESCRIPTION
    Run this after cloning the repo or after the main app setup if you want the
    social service to run as its own startup task.
    It will:
      1. Check for Python 3.11+ and uv
      2. Install dependencies via uv
      3. Create .env from template if needed
      4. Generate SOCIAL_SECRET_KEY if needed
      5. Install the Social API startup task
.EXAMPLE
    .\install_social.ps1
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

Write-Host ""
Write-Host "  ╔══════════════════════════════════════╗" -ForegroundColor Magenta
Write-Host "  ║     GigaLib Social Installer        ║" -ForegroundColor Magenta
Write-Host "  ╚══════════════════════════════════════╝" -ForegroundColor Magenta
Write-Host ""

Write-Step 1 "Checking prerequisites"

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

$uvPath = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvPath) {
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
Write-Ok ('uv ' + $uvVersion)

Write-Step 2 "Installing dependencies"

uv sync
if ($LASTEXITCODE -ne 0) {
    Write-Err "uv sync failed"
    exit 1
}
Write-Ok "All packages installed"

Write-Step 3 "Configuring social environment"

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Ok "Created .env from template"
} else {
    Write-Ok ".env already exists"
}

$envContent = Get-Content .env -Raw
if ($envContent -match "SOCIAL_SECRET_KEY=your-social-service-secret-key") {
    $secretKey = New-RandomSecret
    Set-EnvValue -Path ".env" -Key "SOCIAL_SECRET_KEY" -Value $secretKey
    Write-Ok "Generated random SOCIAL_SECRET_KEY"
}

if ($envContent -match "^SOCIAL_DATABASE_URL=.*sqlite:///gigalib-social\.db" -or $envContent -notmatch "^SOCIAL_DATABASE_URL=.*") {
    Write-Host ""
    Write-Host "  You can keep the default local database or set a custom path." -ForegroundColor White
    $dbValue = Read-Host "  Enter SOCIAL_DATABASE_URL or press Enter to keep the default"
    if ($dbValue) {
        Set-EnvValue -Path ".env" -Key "SOCIAL_DATABASE_URL" -Value $dbValue
        Write-Ok "SOCIAL_DATABASE_URL saved to .env"
    }
}

$useLocal = Read-Host "  Use the local Social API on this PC? (y/n)"
if ($useLocal -eq "y") {
    Set-EnvValue -Path ".env" -Key "GIGALIB_SOCIAL_URL" -Value "http://127.0.0.1:8081"
    Set-EnvValue -Path ".env" -Key "SOCIAL_HOST" -Value "127.0.0.1"
    Set-EnvValue -Path ".env" -Key "SOCIAL_PORT" -Value "8081"
    Write-Ok "Configured local Social API URL"
}

Write-Step 4 "Installing social startup task"

uv run python scripts/install_service.py install --target social
if ($LASTEXITCODE -ne 0) {
    Write-Err "Startup task installation failed"
    exit 1
}
Write-Ok "Social startup task installed"

$startNow = Read-Host "  Start the Social API now? (y/n)"
if ($startNow -eq "y") {
    schtasks /run /tn "GigaLib Social" | Out-Null
    Write-Ok "Social API start request sent"
}

Write-Step 5 "Done!"

Write-Host ""
Write-Host "  Social API is ready." -ForegroundColor Green
Write-Host "  Health check: Invoke-RestMethod http://127.0.0.1:8081/health" -ForegroundColor White
Write-Host "  If you want the main app too, run scripts/setup.ps1 separately." -ForegroundColor DarkGray
Write-Host ""
