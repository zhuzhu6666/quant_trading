@echo off
REM Stop Quant Backend (kills python -m backend process)
taskkill /FI "WINDOWTITLE eq Quant Backend*" /T /F 2>nul
wmic process where "name='python.exe' and commandline like '%%-m backend%%'" delete 2>nul
echo Backend stopped.
