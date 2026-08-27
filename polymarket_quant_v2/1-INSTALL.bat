@echo off
REM ===================================================================
REM  POLYMARKET QUANT BRIDGE  -  V3 rebuild of the V2 installation
REM
REM  One-click install check, research pass, and dashboard.
REM
REM  Read-only against your existing data. Writes only to var\.
REM  Nothing dials out unless you run COLLECT.bat.
REM  Live trading stays disabled until you authorise it by hand.
REM  Safe to run repeatedly. Safe to cancel with Ctrl-C at any point.
REM ===================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist var mkdir var
set "LOG=var\install-run.log"
break > "%LOG%"

REM --- Starting bankroll ----------------------------------------------
REM  The wallet size is CONFIGURED, never assumed. Change it here, or set
REM  PQV3_STARTING_CAPITAL before running this file, or pass --capital.
if not defined PQV3_STARTING_CAPITAL set "PQV3_STARTING_CAPITAL=100"

echo.
echo ============================================================
echo   POLYMARKET QUANT BRIDGE V3
echo   Rebuild of the V2 installation
echo ------------------------------------------------------------
echo   Starting bankroll : $%PQV3_STARTING_CAPITAL%
echo   Live trading      : DISABLED ^(human authorisation required^)
echo   Network           : OFF ^(run COLLECT.bat to enable capture^)
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
echo [ 1/11 ] Python %PYVER% OK  ^(standard library only - nothing to install^)

REM --- 2. Tests -------------------------------------------------------
echo [ 2/11 ] Running the test suite ^(offline, no database needed^)...
%PY% -m pytest tests/ -q >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [FAIL] Tests did not pass. See %LOG%
  echo        Tests need no database, so this is a portability problem
  echo        rather than a configuration one. Send us the log.
  goto :fail
)
findstr /C:"passed" "%LOG%"

REM --- 3. Installation check -----------------------------------------
echo [ 3/11 ] Checking the V3 engine and your data...
%PY% -m pqv3 selftest > var\_selftest.txt 2>&1
type var\_selftest.txt
type var\_selftest.txt >> "%LOG%"
findstr /C:"V1 tape reachable" var\_selftest.txt | findstr /C:"[no ]" >nul
if not errorlevel 1 (
  echo.
  echo [FAIL] Could not reach the database. Expected at:
  echo          ..\Polymarket-Bot-DATA\state\intel.sqlite3
  echo.
  echo        If it lives elsewhere, set the path and re-run this file:
  echo          set PQV3_DATA_DB=D:\path\to\intel.sqlite3
  echo.
  goto :fail
)
del var\_selftest.txt >nul 2>&1

REM --- 4. What evidence exists ----------------------------------------
echo [ 4/11 ] inventory  - what evidence actually exists
%PY% -m pqv3 inventory
%PY% -m pqv3 inventory >> "%LOG%" 2>&1

REM --- 5. The capital model at YOUR bankroll --------------------------
echo.
echo [ 5/11 ] capital    - proving the model at $%PQV3_STARTING_CAPITAL%, including where it refuses
%PY% -m pqv3 capital
%PY% -m pqv3 capital >> "%LOG%" 2>&1

REM --- 6-8. V3 research pass ------------------------------------------
echo.
echo ------------------------------------------------------------
echo   V3 research pass. Roughly 2 minutes.
echo ------------------------------------------------------------
echo [ 6/11 ] startup    - the 15-step startup sequence
%PY% -m pqv3 startup --max-wallets 60
%PY% -m pqv3 startup --max-wallets 60 >> "%LOG%" 2>&1

echo [ 7/11 ] dna        - wallet behavioural fingerprints
%PY% -m pqv3 dna --max-wallets 60 --top 15
%PY% -m pqv3 dna --max-wallets 60 >> "%LOG%" 2>&1

echo [ 8/11 ] scan       - every eligible market, then full decisions on the top 5
%PY% -m pqv3 scan --top 10 --decide 5 --max-wallets 60
%PY% -m pqv3 scan --top 25 --decide 5 --max-wallets 60 >> "%LOG%" 2>&1

REM --- 9. V2 research pass (preserved, still runs) ---------------------
echo.
echo [ 9/11 ] V2 pass    - the preserved research tools ^(slow step^)
%PY% -m pqv2 inventory  >> "%LOG%" 2>&1
%PY% -m pqv2 features   >> "%LOG%" 2>&1
%PY% -m pqv2 rn1        >> "%LOG%" 2>&1
%PY% -m pqv2 discover -v >> "%LOG%" 2>&1
%PY% -m pqv2 winners    >> "%LOG%" 2>&1
%PY% -m pqv2 exits      >> "%LOG%" 2>&1
%PY% -m pqv2 shadow     >> "%LOG%" 2>&1
%PY% -m pqv2 gui --no-open >> "%LOG%" 2>&1
echo          done - see var\dashboard.html for the V2 report

REM --- 10. Forensics ---------------------------------------------------
echo [10/11 ] forensics  - loss classification and missed opportunities
%PY% -m pqv3 forensics
%PY% -m pqv3 forensics >> "%LOG%" 2>&1

REM --- 10b. Results -----------------------------------------------------
echo.
echo ------------------------------------------------------------
echo   Handing over to STEP 3 - RESULTS
echo ------------------------------------------------------------
call "%~dp03-RESULTS.bat" nopause

REM --- 11. Dashboard ---------------------------------------------------
echo.
echo ============================================================
echo   DONE
echo ============================================================
echo.
echo   Live dashboard   : double-click 4-DASHBOARD.vbs
echo                      ^(or: python -m pqv3 dashboard^)
echo   V2 static report : var\dashboard.html
echo   Full transcript  : %LOG%
echo.
echo   Starting bankroll is $%PQV3_STARTING_CAPITAL%. To change it:
echo     set PQV3_STARTING_CAPITAL=250
echo   or per run:
echo     python -m pqv3 --capital 250 scan
echo.
echo   NEXT STEP: double-click 2-COLLECT.bat to capture live data
echo             and re-run the research on it.
echo.
echo   Read next, in this order:
echo     README.md                    what this is and what it refuses to do
echo     HANDOVER.md                  the whole story, two pages
echo     docs\ENGINE-LIMITS.md        what this data CANNOT answer
echo     docs\ENGINE-ARCHITECTURE.md  how it is put together
echo     docs\ENGINE-PERFORMANCE.md   what was actually slow, and why no Rust
echo.
echo   ASK IT ANYTHING
echo     Double-click 6-ASK-THE-AI.bat, or open the dashboard's CHAT page.
echo     Type an ordinary question - "audit the entire system", "why is
echo     the news panel empty", "what should I do next". Answers come from
echo     YOUR data and need no internet and no AI account.
echo     Connect a local model (see MODEL SETUP inside 6-ASK-THE-AI.bat)
echo     and it can change this program for you as well.
echo.
echo   Live trading is DISABLED. To review the requirements:
echo     python -m pqv3 authorize-live
echo   Nothing is authorised until you add --yes.
echo.
echo   Your original Polymarket-Bot-DAVID installation was NOT modified.
echo.
if not defined PQV3_NO_PAUSE (
  echo   Press any key to open the live dashboard...
  pause >nul
  start "" "%~dp04-DASHBOARD.vbs"
)
exit /b 0

:fail
echo.
echo ------------------------------------------------------------
echo   Install check failed. Nothing was changed.
echo   Log: %LOG%
echo ------------------------------------------------------------
echo.
if not defined PQV3_NO_PAUSE pause
exit /b 1
