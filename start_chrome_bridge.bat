@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv || exit /b 1
call ".venv\Scripts\activate.bat"
python -c "import fastapi, google.genai, uvicorn" >nul 2>nul || pip install -r requirements.txt || exit /b 1
python -m uvicorn chrome_server:app --host 127.0.0.1 --port 8765
