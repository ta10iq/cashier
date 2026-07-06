@echo off
setlocal EnableExtensions

set "APP_DIR=%~dp0"
set "APP_FILE=%APP_DIR%app.py"
set "RULE_NAME=Cashier POS 3737"
set "PORT=3737"
set "LOCAL_URL=http://127.0.0.1:3737"

title Cashier POS Server

net session >nul 2>&1
if not "%errorlevel%"=="0" (
    echo Requesting Administrator permission...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%APP_DIR%"

echo ==========================================
echo Cashier POS setup and run
echo ==========================================
echo Folder: %APP_DIR%
echo Local URL : %LOCAL_URL%
echo Port      : %PORT%
echo.

if not exist "%APP_FILE%" (
    echo ERROR: app.py was not found in this folder.
    pause
    exit /b 1
)

where python >nul 2>&1
if not "%errorlevel%"=="0" (
    echo ERROR: Python was not found in PATH.
    echo Install Python or add it to PATH, then run this script again.
    pause
    exit /b 1
)

echo Adding or verifying Windows Firewall rule...
netsh advfirewall firewall show rule name="%RULE_NAME%" >nul 2>&1
if not errorlevel 1 (
    echo Firewall rule already exists: %RULE_NAME%
) else (
    netsh advfirewall firewall add rule name="%RULE_NAME%" dir=in action=allow protocol=TCP localport=%PORT% profile=any
    if errorlevel 1 (
        echo ERROR: Failed to add firewall rule.
        pause
        exit /b 1
    ) else (
        echo Firewall rule added successfully: %RULE_NAME%
    )
)

echo.
echo Checking whether port %PORT% is already in use...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    echo WARNING: Port %PORT% is already used by process %%P.
    echo Close that process first if the cashier app does not start.
)

echo.
echo Starting Cashier POS...
echo Open in your browser: %LOCAL_URL%
echo.
echo Keep this window open while the system is running.
echo Press Ctrl+C to stop.
echo ==========================================
echo.

python "%APP_FILE%"

echo.
echo Cashier POS stopped.
pause
