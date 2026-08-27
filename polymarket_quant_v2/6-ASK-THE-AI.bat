@echo off
REM ===================================================================
REM  STEP 6 - ASK THE AI
REM
REM  Opens a conversation with the intelligence built into this system.
REM  Type an ordinary question or an instruction and press Enter.
REM
REM      audit the entire system
REM      why is the news panel empty?
REM      what should I do next?
REM      analyse every wallet
REM
REM  Answers are COMPUTED from your own data. No internet connection and
REM  no AI subscription is needed for anything above.
REM
REM  If you connect a local model (see MODEL SETUP below) it gains the
REM  ability to change this program for you - open the files, make the
REM  edit, run the tests, and tell you what it changed and how to undo
REM  it. Then instructions like these start working too:
REM
REM      the news collector is broken, find out why and fix it
REM      add a column to the wallets page showing average hold time
REM      find your weakest research step and improve it
REM
REM  TO SEE A CHANGE BEFORE IT IS MADE, close this window and run:
REM
REM      python -m pqv3 agent "your instruction here" --dry-run
REM
REM  That prints the exact edits it would apply and writes nothing.
REM  Without --dry-run it makes the change, runs the tests, and prints
REM  the one command that undoes everything it did.
REM
REM  Type exit to leave. Nothing here can place a trade or turn on live
REM  trading - that stays a separate, deliberate human action.
REM ===================================================================
setlocal
cd /d "%~dp0"

REM --------------------------------------------------------------- MODEL SETUP
REM Optional. Leave these commented out and the console still works in full -
REM it just answers from measured data without a model to converse through.
REM
REM To enable the AI that can CHANGE the program, install Ollama or LM
REM Studio, start it, pull a tool-capable coding model, then remove the
REM REM from the four lines below.
REM
REM     ollama pull qwen2.5-coder:14b
REM
REM Any OpenAI-compatible endpoint works here, local or remote. The model
REM must support tool calling - most current instruct models do.

REM set "PQV3_LLM_PROVIDER=ollama"
REM set "PQV3_LLM_ENDPOINT=http://127.0.0.1:11434/v1"
REM set "PQV3_LLM_MODEL=qwen2.5-coder:14b"
REM set "PQV3_LLM_CONTEXT=32768"

set "PY=python"
%PY% --version >nul 2>&1 || set "PY=py -3"
%PY% --version >nul 2>&1
if errorlevel 1 (
  echo.
  echo [FAIL] Python was not found on PATH. Run 1-INSTALL.bat first.
  echo.
  pause
  exit /b 1
)

echo.
echo  ============================================================
echo   POLYMARKET QUANT BRIDGE - AI CONSOLE
echo  ============================================================
echo.
if defined PQV3_LLM_MODEL (
  echo   Model      : %PQV3_LLM_MODEL%
  echo   It can read and CHANGE this program. Every change it makes
  echo   is recorded, and every session prints how to undo it.
) else (
  echo   Model      : none configured - answers are computed from your
  echo                own data. Open this file in Notepad and read the
  echo                MODEL SETUP section to enable the AI that can
  echo                change the program.
)
echo.
echo   The same console is on the dashboard's CHAT page.
echo.

%PY% -m pqv3 chat

echo.
pause
