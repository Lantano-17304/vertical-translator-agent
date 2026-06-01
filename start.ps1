# -*- coding: utf-8 -*-
# One-click start: backend :8000 + frontend :3000
param(
    [switch]$SkipInstall,
    [switch]$NoBrowser,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = $PSScriptRoot
if (-not $Root) { $Root = (Get-Location).Path }

$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($machinePath -or $userPath) {
    $env:Path = @($machinePath, $userPath) -join ";"
}

$BackendDir = Join-Path $Root "backend"
$FrontendDir = Join-Path $Root "frontend"
$VenvDir = Join-Path $BackendDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip = Join-Path $VenvDir "Scripts\pip.exe"
$EnvFile = Join-Path $Root ".env"
$EnvExample = Join-Path $Root ".env.example"
$RunBackend = Join-Path $Root "scripts\run-backend.ps1"
$RunFrontend = Join-Path $Root "scripts\run-frontend.ps1"

function Write-Info([string]$Message) { Write-Host $Message -ForegroundColor Cyan }
function Write-Warn([string]$Message) { Write-Host $Message -ForegroundColor Yellow }
function Write-Err([string]$Message) { Write-Host $Message -ForegroundColor Red }

function Get-PythonCommand {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        try {
            $v = & python -c "import sys; print('%d.%d' % (sys.version_info.major, sys.version_info.minor))" 2>$null
            if ($v -and [version]$v -ge [version]"3.10") { return @{ Cmd = "python"; Args = @() } }
        } catch { }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @{ Cmd = "py"; Args = @("-3") }
    }
    return $null
}

function Test-PortListening([int]$Port) {
    try {
        if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
            $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
            if ($conn) { return $true }
        }
    } catch { }
    $pattern = ":$Port\s"
    return [bool](netstat -ano 2>$null | Select-String $pattern)
}

function Stop-PortListeners([int[]]$Ports) {
    foreach ($port in $Ports) {
        $stopped = $false
        try {
            if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
                foreach ($c in (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
                    $procId = $c.OwningProcess
                    if ($procId) {
                        Write-Info "Stop PID $procId on port $port"
                        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                        $stopped = $true
                    }
                }
            }
        } catch { }
        if (-not $stopped) {
            foreach ($line in (netstat -ano 2>$null | Select-String ":$port\s")) {
                $parts = ($line.ToString() -split '\s+') | Where-Object { $_ }
                $procId = [int]$parts[-1]
                if ($procId -gt 0) {
                    Write-Info "Stop PID $procId on port $port"
                    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                }
            }
        }
    }
}

function Wait-BackendReady([int]$MaxSeconds = 90) {
    $deadline = (Get-Date).AddSeconds($MaxSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/" -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { return $true }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    return $false
}

if ($Stop) {
    Stop-PortListeners @(8000, 3000)
    Write-Info "Stopped listeners on 8000 and 3000 (if any)."
    exit 0
}

Write-Host ""
Write-Host "  Translator Agent" -ForegroundColor Green
Write-Host "  $Root" -ForegroundColor DarkGray
Write-Host ""

if (-not (Test-Path $EnvFile) -and (Test-Path $EnvExample)) {
    Copy-Item $EnvExample $EnvFile
    Write-Warn "Created .env from .env.example - please set OPENAI_API_KEY."
}

$python = Get-PythonCommand
if (-not $python) {
    Write-Err "Python 3.10+ not found. Install Python and enable Add to PATH, then retry."
    exit 1
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Err "Node.js not found. Install from https://nodejs.org then retry."
    exit 1
}

if ((Test-PortListening 8000) -or (Test-PortListening 3000)) {
    Write-Warn "Port 8000 or 3000 is in use. Run: start.bat stop"
    if ($Host.Name -eq "ConsoleHost") {
        $ans = Read-Host "Continue anyway? (y/N)"
        if ($ans -notmatch '^[yY]') { exit 1 }
    }
}

if (-not $SkipInstall) {
    Write-Info "[1/3] Python venv..."
    if (-not (Test-Path $VenvDir)) {
        $venvArgs = @($python.Args + @("-m", "venv", $VenvDir))
        & $python.Cmd @venvArgs
        if (-not (Test-Path $VenvPython)) {
            Write-Err "Failed to create venv at $VenvDir"
            exit 1
        }
    }

    Write-Info "[2/3] pip install (may take a few minutes)..."
    & $VenvPip install -r (Join-Path $BackendDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Write-Err "pip install failed with code $LASTEXITCODE"
        exit 1
    }

    Write-Info "[3/3] npm install..."
    Push-Location $FrontendDir
    npm install
    $npmCode = $LASTEXITCODE
    Pop-Location
    if ($npmCode -ne 0) {
        Write-Err "npm install failed with code $npmCode"
        exit 1
    }
    Write-Host ""
}
elseif (-not (Test-Path $VenvPython)) {
    Write-Err "Missing backend\.venv - run start.bat once without SkipInstall."
    exit 1
}

if (-not (Test-Path $RunBackend)) {
    Write-Err "Missing $RunBackend"
    exit 1
}
if (-not (Test-Path $RunFrontend)) {
    Write-Err "Missing $RunFrontend"
    exit 1
}

Write-Info "Starting backend and frontend in new windows..."
Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-File", $RunBackend | Out-Null
Start-Sleep -Seconds 2
Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-File", $RunFrontend | Out-Null

Write-Info "Waiting for backend (up to 90s)..."
if (Wait-BackendReady) {
    Write-Host ""
    Write-Host "  Ready" -ForegroundColor Green
    Write-Host "  UI:      http://localhost:3000"
    Write-Host "  API:     http://127.0.0.1:8000"
    Write-Host "  Stop:    close Backend/Frontend windows, or start.bat stop"
    Write-Host ""
    if (-not $NoBrowser) {
        Start-Process "http://localhost:3000"
    }
    exit 0
}

Write-Warn "Backend did not respond in time. Check the Translator Backend window."
Write-Host "  Tips: finish pip install, free ports 8000/3000, set API key in .env"
exit 1
