@echo off
REM ===================================================================
REM  COLLECT + RE-RESEARCH
REM
REM  Run this AFTER INSTALL.bat, then as often as you like.
REM
REM  1. captures live data      (THE ONLY PART THAT USES THE NETWORK)
REM  2. repairs settlement timestamps from the venue
REM  3. re-runs discovery IF the data actually improved
REM  4. rewrites the research report
REM
REM  RUNTIME: about 15 minutes when there is new data to search, of
REM  which ~8 minutes is the discovery pass. When nothing has changed,
REM  step 4 skips itself in seconds and the whole thing takes ~2
REM  minutes - so running this repeatedly is cheap.
REM
REM  No credentials are sent. No orders are placed. Live trading stays
REM  disabled regardless of what this finds.
REM ===================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist var mkdir var
set "LOG=var\collect-run.log"
break > "%LOG%"

set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY py --version >nul 2>&1 && set "PY=py"
if not defined PY (
  echo [FAIL] Python was not found on PATH. Run INSTALL.bat first.
  goto :fail
)

echo.
echo ============================================================
echo   COLLECT + RE-RESEARCH
echo ------------------------------------------------------------
echo   This step makes outbound HTTPS requests to Polymarket.
echo   Everything else in this system is offline.
echo ============================================================
echo.

REM --- 1. capture -----------------------------------------------------
echo [ 1/5 ] Capturing live data ^(markets, order book, news, chain^)...
%PY% -m pqv3 collect --enable
%PY% -m pqv3 collect --enable >> "%LOG%" 2>&1
echo.

REM --- 2. repair ------------------------------------------------------
echo [ 2/5 ] Repairing settlement timestamps from the venue...
echo         Until settlement times are known, the capital simulation
echo         uses an ASSUMED holding period and ten search features
echo         stay inert.
echo.
echo         NOTE: on the supplied historical database this finds NO
echo         matches - those markets are not in the venue's public
echo         catalogue. It is not a failure and re-running will not
echo         change it. Markets that resolve while collection is
echo         RUNNING do get accurate timestamps, so the fix is to
echo         leave it running, not to repeat this step.
echo         ^(see docs\ENGINE-LIMITS.md section 2^)
%PY% -m pqv3 collect --enable --backfill-settled
%PY% -m pqv3 collect --enable --backfill-settled >> "%LOG%" 2>&1
echo.

REM --- 3. coverage ----------------------------------------------------
echo [ 3/5 ] Settlement coverage after the repair:
%PY% -m pqv3 inventory 2>nul | findstr /C:"usable" /C:"coverage" /C:"by method"
%PY% -m pqv3 inventory >> "%LOG%" 2>&1
echo.

REM --- 4. re-research, ONLY if the data moved -------------------------
echo [ 4/5 ] Re-running discovery if the inputs changed...
echo         Skips in seconds when they did not - a pass over unchanged
echo         data reproduces its own previous answer.
%PY% -m pqv3 discover --if-changed
%PY% -m pqv3 discover --if-changed >> "%LOG%" 2>&1
if errorlevel 1 (
  echo.
  echo [WARN] Discovery reported an error. See %LOG%
  echo        Collection above still succeeded; the captured data is saved.
)
echo.

REM --- 5. results -----------------------------------------------------
echo [ 5/5 ] Handing over to STEP 3 - RESULTS...
call "%~dp03-RESULTS.bat" nopause

echo.
echo ============================================================
echo   DONE
echo ============================================================
echo.
echo   Report      : var\reports\RESEARCH-REPORT.md
echo   Results     : re-read any time with 3-RESULTS.bat
echo   Transcript  : %LOG%
echo   Dashboard   : double-click 4-DASHBOARD.vbs
echo.
echo   Order-book history ACCUMULATES - one run captures one snapshot
echo   per token. For continuous capture leave this running instead:
echo.
echo       python -m pqv3 dashboard --loops
echo.
echo   That runs the engine, the collectors and the dashboard together.
echo.
echo   Live trading is still DISABLED. Nothing here can enable it.
echo.
if not defined PQV3_NO_PAUSE pause
exit /b 0

:fail
echo.
echo   Nothing was changed. Log: %LOG%
echo.
if not defined PQV3_NO_PAUSE pause
exit /b 1
