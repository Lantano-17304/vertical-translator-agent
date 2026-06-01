$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendDir = Join-Path $Root "backend"
$VenvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"

$Host.UI.RawUI.WindowTitle = "Translator Backend :8000"
Set-Location $BackendDir

if (-not (Test-Path $VenvPython)) {
    Write-Host "venv not found: $VenvPython" -ForegroundColor Red
    Write-Host "Run start.bat in project root first." -ForegroundColor Yellow
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "Backend: http://127.0.0.1:8000" -ForegroundColor Green
& $VenvPython -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
