@echo off
setlocal
title QC LEAN Bridge
cd /d "%~dp0"

rem ===================================================================
rem  Launcher for QC LEAN Bridge.
rem
rem  This runs the app through Python (which is code-signed by the
rem  Python Software Foundation), so Windows "Smart App Control" allows
rem  it - unlike the standalone .exe, which is unsigned and gets blocked.
rem ===================================================================

rem --- locate a Python launcher ---
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py"
if not defined PYEXE (
  where python >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo.
  echo   Python was not found on this PC.
  echo   Install Python 3.10 or newer from https://www.python.org/downloads/
  echo   ^(tick "Add python.exe to PATH" during setup^), then run this again.
  echo.
  pause
  exit /b 1
)

echo Using Python launcher: %PYEXE%
echo Checking dependencies...
%PYEXE% -c "import PyQt6, pandas, numpy, yaml" 1>nul 2>nul
if errorlevel 1 (
  echo Installing required packages ^(first run only, needs internet^)...
  %PYEXE% -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo.
    echo   Package installation failed. Check your internet connection and retry.
    echo.
    pause
    exit /b 1
  )
)

echo Launching QC LEAN Bridge...
%PYEXE% app.py
if errorlevel 1 (
  echo.
  echo   The app exited with an error. Review the messages above.
  echo.
  pause
)
endlocal
