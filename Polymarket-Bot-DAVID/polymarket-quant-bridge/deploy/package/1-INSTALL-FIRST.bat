@echo off
rem ===================================================================
rem  Top-level installer, so the first thing to click is visible the
rem  moment the zip is opened rather than four folders down.
rem  Hands off to the real setup script.
rem ===================================================================
cd /d "%~dp0"
if not exist "polymarket-quant-bridge\deploy\windows\1-SETUP.bat" (
  echo.
  echo   The folders are not where they should be.
  echo.
  echo   Unzip the WHOLE download into one folder, keeping these
  echo   three together:
  echo       polymarket-quant-bridge
  echo       ploymarketbot
  echo       qc_lean_bridge
  echo.
  pause
  exit /b 1
)
call "polymarket-quant-bridge\deploy\windows\1-SETUP.bat"
