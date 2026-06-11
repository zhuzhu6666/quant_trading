@echo off
setlocal
set PY=C:\Users\zhu\AppData\Local\Programs\Python\Python312\python.exe

cd /d C:\Users\zhu\quant_trading

echo 1/3 Cleaning old backend...
taskkill /F /FI "WINDOWTITLE eq Quant Backend" >nul 2>&1

echo 2/3 Starting backend...
start "Quant Backend" /B "%PY%" -m backend --port 8000

echo 3/3 Starting frontend (Vite :5173)...
cd /d %~dp0frontend-v2
call npx vite

echo Cleaning up backend...
taskkill /F /FI "WINDOWTITLE eq Quant Backend" >nul 2>&1
echo Done.
pause
endlocal
