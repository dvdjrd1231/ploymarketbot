@echo off
REM ===================================================================
REM  STEP 3 - RESULTS
REM
REM  Shows what the system found and why it decided what it decided.
REM  Reads only. Changes nothing. No network. Safe to run any time.
REM
REM  Called automatically at the end of 1-INSTALL.bat and 2-COLLECT.bat,
REM  so you normally do not need to run this yourself. Double-click it
REM  whenever you want to re-read the findings without re-running the
REM  research.
REM
REM  RUNTIME: about 4 minutes.
REM ===================================================================
setlocal
cd /d "%~dp0"

REM Called as `3-RESULTS.bat nopause` by steps 1 and 2 so the sequence does
REM not stop between stages. Passed as an ARGUMENT rather than by setting an
REM environment variable in the caller: the caller would then have to clear it
REM afterwards, which silently destroys a PQV3_NO_PAUSE the operator set
REM themselves. `setlocal` keeps this assignment local to this script.
if /I "%~1"=="nopause" set "PQV3_NO_PAUSE=1"

if not exist var mkdir var
set "LOG=var\results.log"
break > "%LOG%"

set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY py --version >nul 2>&1 && set "PY=py"
if not defined PY (
  echo [FAIL] Python was not found on PATH. Run 1-INSTALL.bat first.
  goto :fail
)

echo.
echo ============================================================
echo   RESULTS
echo   What survived, where candidates go, and whether the
echo   safety gates are helping or costing money.
echo ============================================================

REM --- 1 ---------------------------------------------------------------
echo.
echo ------------------------------------------------------------
echo  [1/4] STRATEGIES - what survived validation, and why the
echo        rest did not
echo ------------------------------------------------------------
echo  Every strategy shows its ladder outcome. Most will say
echo  NEGATIVE, NO_ALPHA or NOT_SIGNIFICANT - that is the system
echo  refusing to promote things that only looked good.
echo.
%PY% -m pqv3 strategies --limit 12
%PY% -m pqv3 strategies --limit 100 >> "%LOG%" 2>&1

REM --- 2 ---------------------------------------------------------------
echo.
echo ------------------------------------------------------------
echo  [2/4] SIGNALS - the pipeline funnel
echo ------------------------------------------------------------
echo  Shows how many observations entered, how many a validated
echo  strategy actually selected, and - for the top few - the
echo  exact gate that refused them.
echo.
echo  This is where you see WHY there are no trades, stage by
echo  stage, instead of just "NO TRADE".
echo.
%PY% -m pqv3 signals --top 5 --limit 5 --decide 3
%PY% -m pqv3 signals --top 20 --limit 40 --decide 5 >> "%LOG%" 2>&1

REM --- 3 ---------------------------------------------------------------
echo.
echo ------------------------------------------------------------
echo  [3/4] INVERT - are the safety gates costing money?
echo ------------------------------------------------------------
echo  Each blocking rule is scored four ways on the same history:
echo  as signalled, inverted ^(buying the opposite contract^),
echo  standing aside, and a coin flip.
echo.
echo  Verdicts you may see:
echo    BLOCK_CORRECT        the gate is earning its place
echo    BLOCK_TOO_STRICT     the gate is refusing profitable trades
echo    PREDICTIVE_INVERTED  the signal is real but points the
echo                         other way
echo    CONTRADICTORY        suspicious - both sides look good,
echo                         which usually means too little data
echo.
echo  No historical outcome is ever changed by this. Only the
echo  interpretation of the signal is varied.
echo.
%PY% -m pqv3 invert --top 8
%PY% -m pqv3 invert --top 40 >> "%LOG%" 2>&1

REM --- 4 ---------------------------------------------------------------
echo.
echo ------------------------------------------------------------
echo  [4/4] REPORT - the full written findings
echo ------------------------------------------------------------
%PY% -m pqv3 report
%PY% -m pqv3 report >> "%LOG%" 2>&1

echo.
echo ============================================================
echo   DONE
echo ============================================================
echo.
echo   Full report      : var\reports\RESEARCH-REPORT.md
echo   This transcript  : %LOG%
echo   Live dashboard   : double-click 4-DASHBOARD.vbs
echo.
echo   If something looks wrong, send back %LOG% - it contains
echo   the complete output of all four steps.
echo.
if not defined PQV3_NO_PAUSE pause
exit /b 0

:fail
echo.
echo   Nothing was changed. Log: %LOG%
echo.
if not defined PQV3_NO_PAUSE pause
exit /b 1
