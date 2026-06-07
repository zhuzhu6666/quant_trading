@echo off
REM Quant Web Console — full launcher (backend :8000 + frontend :3000)
setlocal
set PROJECT_ROOT=%~dp0
set PYTHON=C:\Users\zhu\AppData\Local\Programs\Python\Python312\python.exe

cd /d %PROJECT_ROOT%

REM 1) Backend (minimized window)
echo === Starting Quant Backend (port 8000) ===
start "Quant Backend" /min cmd /c "%PYTHON% -m backend --port 8000"

REM 2) Wait for backend to be ready
timeout /t 4 /nobreak >nul

REM 3) Frontend (foreground, blocks terminal; user Ctrl+C to stop)
echo === Starting Quant Frontend (port 3000) ===
cd /d %PROJECT_ROOT%frontend
call npm run dev

REM When frontend exits, backend window continues running
echo === Frontend exited; backend window continues. Run stop.bat to kill backend. ===
endlocal
