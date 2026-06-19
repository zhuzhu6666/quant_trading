@echo off
cd /d "%~dp0"
title Quant Trading
set PY=C:\Users\zhu\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe
set PYW=C:\Users\zhu\AppData\Local\hermes\hermes-agent\venv\Scripts\pythonw.exe
if not exist "%PYW%" (echo [ERROR] pythonw not found & pause & exit /b 1)
if not exist "start-all.py" (echo [ERROR] start-all.py not found & pause & exit /b 1)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr LISTENING') do taskkill /f /pid %%a >nul 2>&1
start /b "" "%PYW%" start-all.py --prod
echo Starting...
:loop
"%PY%" -c "import urllib.request as u; u.urlopen('http://localhost:8000/api/health',timeout=2)" >nul 2>&1
if errorlevel 1 (timeout /t 2 /nobreak >nul & goto loop)
start http://localhost:8000
echo System started. Close this window to stop.
pause
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr LISTENING') do taskkill /f /pid %%a >nul 2>&1
echo Stopped
