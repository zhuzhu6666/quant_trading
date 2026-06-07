@echo off
REM Quant Web Console — prod launcher (single port :8000)
REM 1. Build frontend for static export
REM 2. Copy to backend/static/
REM 3. Start uvicorn on :8000 (serves API + static)
setlocal
set PROJECT_ROOT=%~dp0
set PYTHON=C:\Users\zhu\AppData\Local\Programs\Python\Python312\python.exe

cd /d %PROJECT_ROOT%

echo === Building frontend for static export ===
cd frontend
call npm run build
if errorlevel 1 (
  echo Frontend build failed.
  exit /b 1
)
cd ..

echo === Copying static output to backend/static/ ===
if exist backend\static rmdir /s /q backend\static
mkdir backend\static
xcopy /e /i /y frontend\out\* backend\static\ >nul

echo === Starting uvicorn on port 8000 (serves API + static frontend) ===
%PYTHON% -m backend --port 8000
echo === Uvicorn exited. ===
endlocal
