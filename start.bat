@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
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

"%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo The existing .venv uses Python older than 3.10. Recreate it with Python 3.10 or later.
    call :handle_error
    exit /b 1
)

echo Checking dependencies...
"%PYTHON_EXE%" -c "import google.genai, numpy, soundcard, soundfile, scipy, yaml, pystray, PIL" >nul 2>nul

if errorlevel 1 (
    echo Installing dependencies...
    "%PYTHON_EXE%" -m pip install --upgrade pip
    if errorlevel 1 (
        echo Failed to upgrade pip.
        call :handle_error
        exit /b 1
    )

    "%PYTHON_EXE%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install dependencies.
        call :handle_error
        exit /b 1
    )
) else (
    echo Dependencies are already installed.
)

echo Starting Gemini Live Translator...
"%PYTHON_EXE%" app.py
if errorlevel 1 call :handle_error
exit /b %errorlevel%

:handle_error
rem Keep errors visible for a normal batch launch, but never leave an
rem invisible paused console behind when the VBS launcher is used.
if not defined GEMINI_TRANSLATOR_HIDDEN pause
exit /b 1
