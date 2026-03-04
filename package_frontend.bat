@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo Frontend Split Packager (Windows One-Click)
echo ============================================================
echo.
echo Notes:
echo - Press Enter: use default auto-detect API logic
echo - Input URL  : force backend API base for split deployment
echo   Example: https://api.your-domain.com
echo.

set "PY_CMD="
if exist ".venv\Scripts\python.exe" (
    set "PY_CMD=.venv\Scripts\python.exe"
) else (
    set "PY_CMD=python"
)

"%PY_CMD%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python or create .venv first.
    pause
    exit /b 1
)

set /p API_BASE=Enter backend API base (leave empty for default): 

if "%API_BASE%"=="" (
    echo.
    echo [RUN] Build package with default API logic...
    "%PY_CMD%" scripts\package_frontend.py
) else (
    echo.
    echo [RUN] Build package with fixed API base: %API_BASE%
    "%PY_CMD%" scripts\package_frontend.py --api-base "%API_BASE%"
)

if errorlevel 1 (
    echo.
    echo [FAILED] Packaging failed. Check API base or script output.
    pause
    exit /b 1
)

echo.
echo [OK] Packaging completed.
echo - Directory: dist\frontend_split
echo - Zip file : dist\frontend_split_package.zip
echo.
pause
