@echo off
title Polymarket Quant Bridge - Wallet ranking
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" ( echo Run 1-SETUP.bat first. & pause & exit /b 1 )
echo.
echo  Wallets the bot found by itself, ranked on measured results.
echo  Nothing here was configured by hand.
echo.
".venv\Scripts\python.exe" -m pqb.cli wallets --limit 30
echo.
pause
