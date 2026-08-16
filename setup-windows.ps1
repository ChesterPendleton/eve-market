# One-shot bootstrap for Windows. No Docker or virtualization required:
# PostgreSQL installs natively and Redis is optional (the app falls back to
# an in-memory cache and says so in `doctor` as "degraded" — that's fine).
#
#     powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1
#
# Safe to re-run: every step checks before it acts.

$ErrorActionPreference = "Stop"
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "    $m" -ForegroundColor Red }

Set-Location $PSScriptRoot

# --- 1. Python -------------------------------------------------------------
Step "Checking Python"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Fail "python not found. Install Python 3.11+ from python.org and tick 'Add python.exe to PATH'."
    exit 1
}
$ver = (python -c "import sys; print('%d.%d' % sys.version_info[:2])")
if ((python -c "import sys; print(1 if sys.version_info >= (3,11) else 0)") -ne "1") {
    Fail "Python 3.11+ required; found $ver"; exit 1
}
Ok "Python $ver"

# --- 2. Virtualenv and dependencies ---------------------------------------
Step "Installing dependencies (takes a minute)"
if (-not (Test-Path ".venv")) { python -m venv .venv }
$vpy = ".\.venv\Scripts\python.exe"
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet -e ".[dev,ui]"
Ok "installed into .venv"
$evemarket = ".\.venv\Scripts\eve-market.exe"

# --- 3. Find or install PostgreSQL ----------------------------------------
Step "Looking for PostgreSQL"
$psql = $null
$c = Get-Command psql -ErrorAction SilentlyContinue
if ($c) { $psql = $c.Source }
if (-not $psql) {
    $f = Get-ChildItem "C:\Program Files\PostgreSQL" -Filter psql.exe -Recurse -ErrorAction SilentlyContinue |
         Sort-Object FullName -Descending | Select-Object -First 1
    if ($f) { $psql = $f.FullName }
}
if (-not $psql) {
    Warn "PostgreSQL not installed. Launching the installer via winget."
    Warn "IMPORTANT: remember the password you set for the 'postgres' user."
    winget install -e --id PostgreSQL.PostgreSQL.16 --accept-package-agreements --accept-source-agreements
    $f = Get-ChildItem "C:\Program Files\PostgreSQL" -Filter psql.exe -Recurse -ErrorAction SilentlyContinue |
         Sort-Object FullName -Descending | Select-Object -First 1
    if ($f) { $psql = $f.FullName }
    if (-not $psql) { Fail "Cannot find psql.exe. Install PostgreSQL, then re-run."; exit 1 }
}
Ok "psql at $psql"

# --- 4. Role and databases -------------------------------------------------
Step "Setting up databases"
$sec = Read-Host "Password for the PostgreSQL 'postgres' superuser" -AsSecureString
$env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))

if ((& $psql -U postgres -h localhost -tAc "SELECT 1 FROM pg_roles WHERE rolname='eve'" 2>$null) -ne "1") {
    & $psql -U postgres -h localhost -c "CREATE ROLE eve LOGIN PASSWORD 'eve' CREATEDB" | Out-Null
    Ok "created role 'eve'"
} else { Ok "role 'eve' exists" }

# The test database is separate on purpose: its tests TRUNCATE, so pointing
# them at the working database would destroy the ledger.
foreach ($db in @("eve_market","eve_market_test")) {
    if ((& $psql -U postgres -h localhost -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" 2>$null) -ne "1") {
        & $psql -U postgres -h localhost -c "CREATE DATABASE $db OWNER eve" | Out-Null
        Ok "created $db"
    } else { Ok "$db exists" }
}
Remove-Item Env:\PGPASSWORD

# --- 5. .env ---------------------------------------------------------------
Step "Configuring .env"
if (Test-Path ".env") {
    Ok ".env exists, leaving it alone"
} else {
    Copy-Item ".env.example" ".env"
    $email = Read-Host "Contact email for the ESI User-Agent (CCP requires this)"
    $lines = Get-Content ".env"
    if ($email) { $lines = $lines -replace '^EVE_CONTACT_EMAIL=.*', "EVE_CONTACT_EMAIL=$email" }
    else { Warn "no email given - edit EVE_CONTACT_EMAIL before going live" }
    # No Redis on Windows without Docker: blank the URL so the in-memory
    # cache is used silently instead of warning on every command.
    $lines = $lines -replace '^EVE_REDIS_URL=.*', 'EVE_REDIS_URL='
    Set-Content ".env" $lines
    Ok "wrote .env"
}

# --- 6. Schema -------------------------------------------------------------
Step "Applying database schema"
& $evemarket migrate
if ($LASTEXITCODE -ne 0) { Fail "migrate failed - check EVE_DATABASE_URL in .env"; exit 1 }

# --- 7. Static data --------------------------------------------------------
Step "Loading item names and cargo volumes (~150MB download)"
& $evemarket fetch-sde
if ($LASTEXITCODE -ne 0) { Warn "fetch-sde failed - re-run 'eve-market fetch-sde' later" }

# --- 8. Verify -------------------------------------------------------------
Step "Running doctor"
& $evemarket doctor

Write-Host @"

Setup done. Redis showing 'degraded' is expected without Docker - the
in-memory cache covers it.

Start the dashboard:

    .\.venv\Scripts\eve-market.exe ui

Or from an activated shell (.\.venv\Scripts\Activate.ps1): eve-market ui
"@ -ForegroundColor Cyan
