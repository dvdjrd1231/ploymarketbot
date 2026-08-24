@echo off
title Polymarket Quant Bridge - Strategy discovery
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" ( echo Run 1-SETUP.bat first. & pause & exit /b 1 )
echo.
echo  ============================================================
echo   STRATEGY DISCOVERY
echo  ============================================================
echo.
echo   This studies the market data the bot has collected and looks
echo   for trading rules that hold up under testing.
echo.
echo   It needs roughly 3-4 HOURS of the bot running first. If you
echo   run it too early it will simply say there is not enough data
echo   yet - that is normal, not a fault.
echo.
echo   It can take several minutes. Leave this window open.
echo.
pause
".venv\Scripts\python.exe" -m pqb.cli research
echo.
pause
