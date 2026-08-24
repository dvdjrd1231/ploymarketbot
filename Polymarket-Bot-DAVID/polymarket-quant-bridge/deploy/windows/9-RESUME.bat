@echo off
title Polymarket Quant Bridge - Resume
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" ( echo Run 1-SETUP.bat first. & pause & exit /b 1 )
".venv\Scripts\python.exe" -m pqb.cli resume
echo.
pause
