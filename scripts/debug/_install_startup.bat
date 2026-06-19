@echo off
cd /d "%~dp0"
schtasks /create /tn QuantTrading /tr "C:\Users\zhu\AppData\Local\hermes\hermes-agent\venv\Scripts\pythonw.exe C:\Users\zhu\quant_trading\start-all.py --prod" /sc onlogon /ru zhu /rl highest /f
echo EXIT_CODE=%errorlevel%
