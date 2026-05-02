@echo off
REM Loan Wizard - one-shot launcher for Windows
setlocal

cd /d "%~dp0backend"

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python not found on PATH. Install Python 3.10+ and try again.
    exit /b 1
)

if not exist ".venv" (
    echo ==^> Creating virtual environment in backend\.venv
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo ==^> Installing dependencies
python -m pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo.
echo ================================================================
echo   Loan Wizard is starting on http://localhost:8000
echo   Admin dashboard at        http://localhost:8000/admin
echo   API docs at               http://localhost:8000/docs
echo   Press Ctrl+C to stop.
echo ================================================================
echo.

uvicorn main:app --host 0.0.0.0 --port 8000

endlocal
