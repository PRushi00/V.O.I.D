@echo off
REM Read-only ListModels capability probe, using the existing .venv.
set PROJ=C:\V.O.I.D
"%PROJ%\.venv\Scripts\python.exe" "%PROJ%\verify\list_models.py"
echo.
echo Done. See verify\models_report.txt
pause
