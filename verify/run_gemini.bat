@echo off
REM Re-run ONLY the Gemini connectivity check, using the existing .venv.
set PROJ=C:\V.O.I.D
"%PROJ%\.venv\Scripts\python.exe" "%PROJ%\verify\check_gemini_only.py"
echo.
echo Done. See verify\gemini_report.txt
pause
