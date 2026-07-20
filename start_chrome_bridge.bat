@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem Replace any stale bridge instance before starting a fresh one.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$procs = Get-CimInstance Win32_Process; foreach ($p in $procs) { if (($p.CommandLine -like '*uvicorn chrome_server:app*') -and ($p.CommandLine -like '*--port 8765*')) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } }"
timeout /t 1 /nobreak >nul

if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv || exit /b 1
call ".venv\Scripts\activate.bat"
python -c "import fastapi, google.genai, uvicorn" >nul 2>nul || pip install -r requirements.txt || exit /b 1
python -m uvicorn chrome_server:app --host 127.0.0.1 --port 8765
