@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Translator Agent 启动器
set "EXITCODE=0"

if /i "%~1"=="stop" (
    echo 正在停止 8000 / 3000 端口上的服务...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Stop
    set "EXITCODE=%ERRORLEVEL%"
    goto :finish
)

echo.
echo ========================================
echo   Translator Agent
echo ========================================
echo.
echo Starting... First run installs deps and may take several minutes.
echo.

where powershell >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 PowerShell，请安装 Windows PowerShell 5.1 或更高版本。
    set "EXITCODE=1"
    goto :finish
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "EXITCODE=%ERRORLEVEL%"

echo.
if "%EXITCODE%"=="0" (
    echo [OK] If Backend / Frontend windows opened, services are running.
    echo      Open: http://localhost:3000
    echo      Stop: close those windows, or run start.bat stop
) else (
    echo [FAILED] Exit code: %EXITCODE%
    echo          Read messages above. Need Python 3.10+ and Node.js in PATH.
)

:finish
echo.
pause
endlocal & exit /b %EXITCODE%
