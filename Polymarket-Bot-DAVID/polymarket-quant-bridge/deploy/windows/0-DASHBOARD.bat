@echo off
rem Fallback launcher for the dashboard, if Windows policy blocks .vbs files.
rem Briefly shows a console, then hands off to the windowed Python and exits.
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" (
  echo Setup has not been run yet - please run 1-SETUP.bat first.
  pause
  exit /b 1
)
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -m pqb.gui
) else (
  start "" ".venv\Scripts\python.exe" -m pqb.gui
)
exit
