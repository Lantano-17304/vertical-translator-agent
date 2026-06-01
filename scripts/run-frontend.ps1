$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$FrontendDir = Join-Path $Root "frontend"

$Host.UI.RawUI.WindowTitle = "Translator Frontend :3000"
Set-Location $FrontendDir

if (-not (Test-Path (Join-Path $FrontendDir "node_modules"))) {
    Write-Host "node_modules not found" -ForegroundColor Red
    Write-Host "Run start.bat in project root first." -ForegroundColor Yellow
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "Frontend: http://localhost:3000" -ForegroundColor Green
npm run dev
