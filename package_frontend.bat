@echo off
setlocal
cd /d "%~dp0"

set "PY_CMD=python"
if exist ".venv\Scripts\python.exe" set "PY_CMD=.venv\Scripts\python.exe"

"%PY_CMD%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python or create .venv first.
    exit /b 1
)

"%PY_CMD%" scripts\package_frontend.py --profile-mode
exit /b %errorlevel%
