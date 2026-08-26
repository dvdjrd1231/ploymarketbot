@echo off
REM Stops the background dashboard server started by DASHBOARD.vbs.
setlocal
echo Stopping any pqv3 dashboard server on port 8787...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:"127.0.0.1:8787 .*LISTENING"') do (
  echo   killing PID %%P
  taskkill /PID %%P /F >nul 2>&1
)
echo Done.
pause
