@echo off
setlocal enabledelayedexpansion

echo [INFO] Starting KokoroGUI...

:: Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    pause
    exit /b 1
)

:: Check Python version is supported. kokoro==0.9.4 requires Python >=3.10,<3.13,
:: and pyproject.toml sets the floor at 3.11, so only 3.11 and 3.12 work.
python -c "import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 12) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] KokoroGUI requires Python 3.11 or 3.12. kokoro==0.9.4 caps the ceiling below 3.13.
    echo [ERROR] Install Python 3.11 or 3.12, make sure it is first on PATH, and re-run this script.
    pause
    exit /b 1
)

:: Set up Virtual Environment if not exists
if not exist .venv (
    echo [INFO] Creating virtual environment...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

:: Activate Venv
call .venv\Scripts\activate

:: Install/Update Requirements
if exist requirements.txt (
    echo [INFO] Checking requirements...
    pip install -r requirements.txt --quiet
)

:: Start Application
echo [INFO] Launching GUI...
python main.py

pause
