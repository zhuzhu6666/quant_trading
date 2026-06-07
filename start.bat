@echo off
REM Quant Web Console — Phase 1 launcher (backend only)
setlocal
set PROJECT_ROOT=%~dp0
set PYTHON=C:\Users\zhu\AppData\Local\Programs\Python\Python312\python.exe

cd /d %PROJECT_ROOT%
echo === Starting Quant Backend (port 8000) ===
%PYTHON% -m backend --port 8000
endlocal
