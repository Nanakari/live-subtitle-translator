@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo Python 3.10 or later is required for the current dependencies.
        call :handle_error
        exit /b 1
    )
    echo Creating virtual environment...
    py -3 -m venv .venv
    if errorlevel 1 (
        echo Failed to create virtual environment. Please install Python 3.
        call :handle_error
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo The existing .venv uses Python older than 3.10. Recreate it with Python 3.10 or later.
    call :handle_error
    exit /b 1
)

echo Checking dependencies...
python -c "import google.genai, numpy, soundcard, soundfile, scipy, yaml, pystray, PIL" >nul 2>nul

if errorlevel 1 (
    echo Installing dependencies...
    python -m pip install --upgrade pip
    if errorlevel 1 (
        echo Failed to upgrade pip.
        call :handle_error
        exit /b 1
    )

    pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install dependencies.
        call :handle_error
        exit /b 1
    )
) else (
    echo Dependencies are already installed.
)

echo Starting Gemini Live Translator...
python app.py
if errorlevel 1 call :handle_error
exit /b %errorlevel%

:handle_error
rem Keep errors visible for a normal batch launch, but never leave an
rem invisible paused console behind when the VBS launcher is used.
if not defined GEMINI_TRANSLATOR_HIDDEN pause
exit /b 1
