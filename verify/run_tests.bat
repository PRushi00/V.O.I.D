@echo off
REM Run the automated test suite on this machine, using the existing .venv.
set PROJ=C:\V.O.I.D
"%PROJ%\.venv\Scripts\python.exe" -m pytest "%PROJ%\tests" -q > "%PROJ%\verify\test_results.txt" 2>&1
type "%PROJ%\verify\test_results.txt"
pause
