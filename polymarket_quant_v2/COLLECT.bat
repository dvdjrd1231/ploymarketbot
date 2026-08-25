@echo off
REM ===================================================================
REM  Live data capture. THIS IS THE ONLY FILE THAT USES THE NETWORK.
REM
REM  Order book, news, chain and market metadata have NO history in this
REM  project and cannot be backfilled - they accumulate from the moment
REM  you start collecting. Until enough exists, the features that depend
REM  on them stay gated and the dashboard says so.
REM ===================================================================
setlocal
cd /d "%~dp0"
set "PY=python"
python --version >nul 2>&1 || set "PY=py"

echo.
echo ============================================================
echo   LIVE CAPTURE
echo   This makes outbound HTTPS requests to Polymarket.
echo   No credentials are sent. No orders are placed.
echo ============================================================
echo.
echo [1/2] Running all collectors once...
%PY% -m pqv3 collect --enable
echo.
echo [2/2] Repairing settlement timestamps from the venue...
echo       ^(this is the highest-value data fix available - see
echo        docs\ENGINE-LIMITS.md section 2^)
%PY% -m pqv3 collect --enable --backfill-settled
echo.
echo Done. Re-run this periodically to accumulate history.
echo Check coverage with:  python -m pqv3 inventory
echo.
pause
