@echo off
setlocal EnableDelayedExpansion
title Polymarket Quant Bridge - Setup
cd /d "%~dp0..\.."

rem ===================================================================
rem  One-time setup for a Windows PC.
rem
rem  Double-click this once. It checks Python, builds a private
rem  environment for the bot, installs everything it needs, creates
rem  your settings file, and then verifies the whole thing.
rem
rem  Safe to run again at any time - it will not overwrite settings
rem  you have changed or any data the bot has collected.
rem ===================================================================

echo.
echo  ============================================================
echo   POLYMARKET QUANT BRIDGE - SETUP
echo  ============================================================
echo.

rem --- 1. the two companion folders --------------------------------
rem Checked FIRST, before anything is installed, because getting this
rem layout wrong is the most common way the install fails - and it is
rem much friendlier to say so now than after a five-minute download.
set "PARENT=%CD%\.."
set "MISSING="

if not exist "%PARENT%\ploymarketbot\backend\services\trading.py" (
  set "MISSING=1"
  echo  [X] Cannot find the "ploymarketbot" folder.
)
if not exist "%PARENT%\qc_lean_bridge\core\pipeline.py" (
  if exist "%PARENT%\Advanced Strategy Development for QuantConnect LEAN\qc_lean_bridge\core\pipeline.py" (
    echo  [i] Found the Quant Bridge inside its outer folder - that is fine.
    setx PQB_QUANT_BRIDGE_PATH "%PARENT%\Advanced Strategy Development for QuantConnect LEAN\qc_lean_bridge" >nul
    set "PQB_QUANT_BRIDGE_PATH=%PARENT%\Advanced Strategy Development for QuantConnect LEAN\qc_lean_bridge"
  ) else (
    set "MISSING=1"
    echo  [X] Cannot find the "qc_lean_bridge" folder.
  )
)

if defined MISSING (
  echo.
  echo  The bot needs THREE folders sitting next to each other:
  echo.
  echo      %PARENT%\
  echo         polymarket-quant-bridge\   ^<- this one
  echo         ploymarketbot\
  echo         qc_lean_bridge\
  echo.
  echo  Please move the folders so they sit side by side, then run
  echo  this setup again.
  echo.
  pause
  exit /b 1
)
echo  [OK] Found both companion folders.

rem --- 2. Python ----------------------------------------------------
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
  where python >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo.
  echo  [X] Python is not installed.
  echo.
  echo      Download it from  https://www.python.org/downloads/
  echo      IMPORTANT: tick "Add python.exe to PATH" during install.
  echo      Then run this setup again.
  echo.
  pause
  exit /b 1
)

%PYEXE% -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo  [X] Python 3.10 or newer is required.
  echo      Please update Python, then run this setup again.
  pause
  exit /b 1
)
for /f "delims=" %%v in ('%PYEXE% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%v"
echo  [OK] Python !PYVER!

rem --- 3. private environment ---------------------------------------
rem Its own folder so the bot's packages cannot clash with anything
rem else installed on this PC.
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo  Creating the bot's private environment...
  %PYEXE% -m venv .venv
  if errorlevel 1 (
    echo  [X] Could not create the environment.
    pause
    exit /b 1
  )
)
set "VPY=%CD%\.venv\Scripts\python.exe"
echo  [OK] Environment ready.

echo.
echo  Installing required packages. First run downloads about 100 MB
echo  and can take a few minutes - please leave this window open.
echo.
"%VPY%" -m pip install --upgrade pip --quiet
"%VPY%" -m pip install -r requirements.txt --quiet
if errorlevel 1 (
  echo  [X] Package install failed. Check your internet connection.
  pause
  exit /b 1
)
"%VPY%" -m pip install -r "%PARENT%\ploymarketbot\requirements.txt" --quiet
if errorlevel 1 (
  echo  [!] Some Polymarket packages failed to install.
  echo      Simulation will still work; live trading may not.
)
rem The dashboard only. Kept separate so a server install skips it entirely.
"%VPY%" -m pip install -r requirements-desktop.txt --quiet
if errorlevel 1 (
  echo  [!] The dashboard window could not be installed.
  echo      Everything still works from the numbered .bat files.
)
echo  [OK] Packages installed.

rem --- 4. settings ---------------------------------------------------
if not exist "config\config.yaml" (
  copy /y "config\config.example.yaml" "config\config.yaml" >nul
  echo  [OK] Created your settings file  ^(config\config.yaml^).
) else (
  echo  [OK] Settings file already exists - left untouched.
)

rem The example ships a self-contained layout with the databases inside this
rem folder. If a Polymarket-Bot-DATA folder was supplied alongside, point the
rem config at it instead - otherwise the bot silently starts against an empty
rem database and ignores the collected history. Only rewrites values still at
rem the template default, so an edited config is never touched.
"%VPY%" "deploy\windows\_configure_paths.py"

if not exist "state" mkdir "state"

rem --- 5. verify ------------------------------------------------------
echo.
echo  ------------------------------------------------------------
echo   Checking everything...
echo  ------------------------------------------------------------
"%VPY%" -m pqb.cli check
if errorlevel 1 (
  echo.
  echo  [X] The check reported a problem - see the messages above.
  pause
  exit /b 1
)

echo.
echo  ============================================================
echo   SETUP COMPLETE
echo  ============================================================
echo.
echo   The bot is in SIMULATION mode. It cannot place a real
echo   order or spend any money until that is deliberately
echo   changed.
echo.
echo   Next: double-click  START-DASHBOARD.vbs
echo.
echo   That opens the dashboard window - no command prompt.
echo   Press "Start bot" inside it.
echo.
pause
