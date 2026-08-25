@echo off
REM ===================================================================
REM  Polymarket Quant Engine V2 - one-click install check and full run
REM
REM  Read-only against the existing installation. Writes only to var\.
REM  Safe to run repeatedly. Safe to cancel with Ctrl-C at any point.
REM ===================================================================
setlocal
cd /d "%~dp0"

if not exist var mkdir var
set "LOG=var\install-run.log"
break > "%LOG%"

echo.
echo ============================================================
echo   POLYMARKET QUANT ENGINE V2
echo   Install check and full research cycle
echo ============================================================
echo.

REM --- 1. Python ------------------------------------------------------
set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY py --version >nul 2>&1 && set "PY=py"
if not defined PY (
  echo [FAIL] Python was not found on PATH.
  echo        Install Python 3.11 or newer from https://python.org
  echo        and tick "Add python.exe to PATH" during setup.
  goto :fail
)
for /f "tokens=2" %%V in ('%PY% --version 2^>^&1') do set "PYVER=%%V"
%PY% -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
if errorlevel 1 (
  echo [FAIL] Python 3.11 or newer is required. Found %PYVER%.
  goto :fail
)
echo [ 1/13 ] Python %PYVER% OK

REM --- 2. Test suite --------------------------------------------------
echo [ 2/13 ] Running the test suite ^(no database needed^)...
%PY% -m pytest tests/ -q >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [FAIL] Tests did not pass. See %LOG%
  echo        Tests need no database, so this is a portability problem
  echo        rather than a configuration one. Send us the log.
  goto :fail
)
findstr /C:"passed" "%LOG%"

REM --- 3. Data access -------------------------------------------------
echo [ 3/13 ] Checking access to your data...
%PY% -m pqv2 selftest > var\_selftest.txt 2>&1
if errorlevel 1 (
  type var\_selftest.txt
  echo.
  echo [FAIL] Could not reach the database. Expected at:
  echo          ..\Polymarket-Bot-DATA\state\intel.sqlite3
  echo.
  echo        If it lives elsewhere, set the path and re-run this file:
  echo          set PQV2_DATA_DB=D:\path\to\intel.sqlite3
  echo.
  goto :fail
)
type var\_selftest.txt >> "%LOG%"
findstr /C:"[PASS]" var\_selftest.txt
del var\_selftest.txt >nul 2>&1

echo.
echo ------------------------------------------------------------
echo   Running the full cycle. Roughly 5 minutes.
echo   Progress below; full transcript in %LOG%
echo ------------------------------------------------------------
echo.

echo [ 4/13 ] audit      - where the existing engine's opportunities go
%PY% -m pqv2 audit >> "%LOG%" 2>&1

echo [ 5/13 ] reconcile  - reconciliation exit safety, before/after
%PY% -m pqv2 reconcile --demo >> "%LOG%" 2>&1

echo [ 6/13 ] inventory  - how much evidence actually exists
%PY% -m pqv2 inventory >> "%LOG%" 2>&1

echo [ 7/13 ] features   - which features inform, which are inert
%PY% -m pqv2 features >> "%LOG%" 2>&1

echo [ 8/13 ] rn1        - reconstructing the reference wallet
%PY% -m pqv2 rn1 >> "%LOG%" 2>&1

echo [ 9/13 ] discover   - discovery + validation ^(the slow step^)
%PY% -m pqv2 discover -v >> "%LOG%" 2>&1

echo [10/13 ] winners    - what separates big winners from big losers
%PY% -m pqv2 winners >> "%LOG%" 2>&1

echo [11/13 ] exits      - hold to settlement, or exit early
%PY% -m pqv2 exits >> "%LOG%" 2>&1

echo [12/13 ] expansion  - Win Expansion ladder and staking modes
%PY% -m pqv2 expansion >> "%LOG%" 2>&1

echo [13/13 ] shadow     - full pipeline replayed over history
%PY% -m pqv2 shadow >> "%LOG%" 2>&1

echo.
echo ============================================================
echo   DASHBOARD
echo ============================================================
%PY% -m pqv2 dashboard
%PY% -m pqv2 dashboard >> "%LOG%" 2>&1

echo.
echo ============================================================
echo   DIAGNOSTIC - the 22 questions
echo ============================================================
%PY% -m pqv2 diagnose
%PY% -m pqv2 diagnose >> "%LOG%" 2>&1

echo.
echo ============================================================
echo   DONE
echo ============================================================
echo.
echo   Full transcript : %LOG%
echo   JSON reports    : var\reports\
echo.
echo   Read next, in this order:
echo     HANDOVER.md          the whole story, two pages
echo     docs\FINDINGS.md     what this run found
echo     docs\PRIOR-WORK.md   two validated strategies nothing reads
echo     docs\LIMITS.md       what this data cannot answer
echo.
echo   Your original system was NOT modified. Verify with:
echo     git status Polymarket-Bot-DAVID/
echo.
if not defined PQV2_NO_PAUSE pause
exit /b 0

:fail
echo.
echo ------------------------------------------------------------
echo   Install check failed. Nothing was changed.
echo   Log: %LOG%
echo ------------------------------------------------------------
echo.
if not defined PQV2_NO_PAUSE pause
exit /b 1
