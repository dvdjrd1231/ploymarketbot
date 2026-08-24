@echo off
title Polymarket Quant Bridge - EMERGENCY STOP
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" ( echo Run 1-SETUP.bat first. & pause & exit /b 1 )
echo.
echo  ============================================================
echo   EMERGENCY STOP
echo  ============================================================
echo.
echo   This stops the bot placing any new orders, within seconds,
echo   even while it is running. Existing positions are left alone.
echo.
echo   Choose:
echo     1 = Stop new orders only
echo     2 = Stop AND close every open position
echo     3 = Cancel, do nothing
echo.
set /p CHOICE="  Enter 1, 2 or 3: "
if "%CHOICE%"=="1" ".venv\Scripts\python.exe" -m pqb.cli kill
if "%CHOICE%"=="2" ".venv\Scripts\python.exe" -m pqb.cli kill --flatten
if "%CHOICE%"=="3" echo   Cancelled.
echo.
echo   To let it trade again later, run 9-RESUME.bat
echo.
pause
