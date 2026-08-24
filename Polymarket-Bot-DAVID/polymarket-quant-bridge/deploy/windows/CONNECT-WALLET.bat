@echo off
setlocal
title Polymarket Quant Bridge - Connect your wallet
cd /d "%~dp0..\.."

rem ===================================================================
rem  Connect a MetaMask (or Trust Wallet) wallet.
rem
rem  This does NOT enable real trading. It works out how your account
rem  is set up, checks it can see your USDC, and saves those two
rem  settings. The bot stays in simulation until you change that
rem  deliberately, somewhere else.
rem
rem  Your private key is NEVER saved into the settings file and is
rem  never shown on screen.
rem ===================================================================

echo.
echo  ============================================================
echo   CONNECT YOUR WALLET
echo  ============================================================
echo.
echo   You need your PRIVATE KEY from MetaMask:
echo.
echo     MetaMask - three dots - Account details
echo              - Show private key - enter your password
echo.
echo   It is 64 characters of 0-9 and a-f.
echo.
echo   NOT your 12-word Secret Recovery Phrase. If you paste the
echo   words, this will stop and tell you so - it will not use them.
echo.
echo  ------------------------------------------------------------
echo.
echo   TIP: if you added money through the Polymarket website, your
echo   USDC probably sits in a Polymarket wallet rather than in
echo   MetaMask itself. That is normal. If it reports 0.00 below,
echo   copy the address shown in the Polymarket app and run:
echo.
echo       CONNECT-WALLET.bat 0xYourPolymarketAddress
echo.
echo  ------------------------------------------------------------
echo.

if not exist ".venv\Scripts\python.exe" (
  echo  [X] The bot is not installed yet.
  echo      Run 1-INSTALL-FIRST.bat first, then come back here.
  echo.
  pause
  exit /b 1
)

rem The key is typed at a hidden prompt, never passed on this command
rem line -- anything typed after the .bat is only ever the FUNDER address,
rem which is public.
if "%~1"=="" (
  ".venv\Scripts\python.exe" -m pqb.cli wallet-connect
) else (
  ".venv\Scripts\python.exe" -m pqb.cli wallet-connect --funder %1
)

echo.
echo  ------------------------------------------------------------
echo   If that found your USDC, you are connected.
echo.
echo   The bot is STILL in simulation. Nothing above turned on
echo   real trading, and nothing here can spend your money.
echo  ------------------------------------------------------------
echo.
pause
