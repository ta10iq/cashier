@echo off
setlocal EnableExtensions

set "SCRIPT=%~dp0setup_iis_reverse_proxy.ps1"
set "DOMAIN=taha-cashier.duckdns.org"

net session >nul 2>&1
if not "%errorlevel%"=="0" (
    echo Requesting Administrator permission...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" -Domain "%DOMAIN%"
pause
