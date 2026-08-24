# Build the standalone Windows executable for QC LEAN Bridge.
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "Installing build dependency (PyInstaller)..." -ForegroundColor Cyan
python -m pip install --quiet --upgrade pyinstaller

Write-Host "Cleaning previous build..." -ForegroundColor Cyan
if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist)  { Remove-Item dist  -Recurse -Force }

Write-Host "Building..." -ForegroundColor Cyan
python -m PyInstaller build.spec --noconfirm

Write-Host ""
Write-Host "Done. Executable:" -ForegroundColor Green
Write-Host "  dist\QC-LEAN-Bridge\QC-LEAN-Bridge.exe"
