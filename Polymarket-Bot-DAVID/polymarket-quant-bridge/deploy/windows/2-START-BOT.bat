@echo off
title Polymarket Quant Bridge - RUNNING (simulation)
cd /d "%~dp0..\.."

rem ===================================================================
rem  Start the bot. It runs in this window until you close it or press
rem  Ctrl+C - closing the window is a clean stop; the bot saves its
rem  state on the way out.
rem ===================================================================

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo  Setup has not been run yet.
  echo  Please double-click 1-SETUP.bat first.
  echo.
  pause
  exit /b 1
)

echo.
echo  ============================================================
echo   POLYMARKET QUANT BRIDGE
echo  ============================================================
echo.
echo   Mode      : as set in config\config.yaml (ships as SIMULATION)
echo   To stop   : press Ctrl+C, or just close this window
echo   Live view : open 3-STATUS.bat in a second window
echo.
echo   The first few minutes look quiet while it learns the market.
echo   Leave it running for several hours before judging it - it is
echo   collecting the history it later studies.
echo.
echo  ------------------------------------------------------------
echo.

".venv\Scripts\python.exe" -m pqb.cli run

echo.
echo  ------------------------------------------------------------
echo   The bot has stopped.
echo  ------------------------------------------------------------
pause
