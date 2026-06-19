@echo off
set QUANT_JWT_SECRET=quant-trading-jwt-secret-key-2026
cd /d C:\Users\zhu\quant_trading
python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000
