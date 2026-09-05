@echo off
REM Live Gemini tool-calling milestone task (read-only), using the existing .venv.
set PROJ=C:\V.O.I.D
"%PROJ%\.venv\Scripts\python.exe" "%PROJ%\verify\live_gemini_task.py"
echo.
echo Done. See verify\live_task_report.txt
pause
