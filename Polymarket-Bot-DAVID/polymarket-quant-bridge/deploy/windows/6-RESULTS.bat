@echo off
title Polymarket Quant Bridge - Results
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" ( echo Run 1-SETUP.bat first. & pause & exit /b 1 )
echo.
echo  What has actually worked so far. Empty until trades have closed.
echo.
".venv\Scripts\python.exe" -m pqb.cli report
echo.
pause
