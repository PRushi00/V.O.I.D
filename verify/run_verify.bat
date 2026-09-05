@echo off
REM Launcher for the V.O.I.D laptop verification. Double-click or run via the
REM Explorer address bar. It calls the PowerShell script beside it.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_verify.ps1"
echo.
echo Verification finished. See verify\verify_report.txt
pause
