@echo off
title Polymarket Quant Bridge - Detections
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" ( echo Run 1-SETUP.bat first. & pause & exit /b 1 )
echo.
echo  Unusual behaviour the bot spotted, with the numbers behind it.
echo.
".venv\Scripts\python.exe" -m pqb.cli anomalies --limit 10
echo.
pause
