@echo off
REM Double-click to start Lighthouse on Windows.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Setting up Lighthouse for the first time...
    where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
    if not exist ".venv\Scripts\python.exe" (
        echo.
        echo Python 3.11+ was not found. Install it from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" during setup, then double-click this file again.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
)
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
".venv\Scripts\python.exe" -m lighthouse start
pause
