@echo off
REM Read-only ListModels capability probe, using the existing .venv.
set PROJ=C:\Users\nanda\OneDrive\Desktop\Studies\AI\Assistant\V.O.I.D
"%PROJ%\.venv\Scripts\python.exe" "%PROJ%\verify\list_models.py"
echo.
echo Done. See verify\models_report.txt
pause
