@echo off
cd /d C:\Users\zhu\quant_trading
set QUANT_JWT_SECRET=my-fixed-dev-jwt-secret-2026
start /B python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000 --log-level warning > logs\backend.log 2>&1
echo BACKEND STARTED
