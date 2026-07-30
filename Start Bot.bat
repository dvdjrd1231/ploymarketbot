@echo off
setlocal
title Polymarket Copy Trading Bot

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   Python was not found on PATH.
    echo   Install Python 3.10+ from https://python.org and tick
    echo   "Add python.exe to PATH" during setup, then run this again.
    echo.
    pause
    exit /b 1
)

if not exist "%APPDATA%\PolymarketBotWeb\.deps-ok" (
    echo Installing Python dependencies ^(first run only^)...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   Dependency installation failed. Check the messages above.
        echo.
        pause
        exit /b 1
    )
    if not exist "%APPDATA%\PolymarketBotWeb" mkdir "%APPDATA%\PolymarketBotWeb"
    echo ok> "%APPDATA%\PolymarketBotWeb\.deps-ok"
)

python run.py %*

if errorlevel 1 pause
endlocal
