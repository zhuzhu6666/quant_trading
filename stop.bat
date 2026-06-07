@echo off
REM Stop Quant Web Console (backend :8000 + frontend :3000)
echo Killing backend (python -m backend)...
taskkill /FI "WINDOWTITLE eq Quant Backend*" /T /F 2>nul
wmic process where "name='python.exe' and commandline like '%%-m backend%%'" delete 2>nul
echo Killing frontend (next dev)...
taskkill /FI "WINDOWTITLE eq Quant Frontend*" /T /F 2>nul
wmic process where "name='node.exe' and commandline like '%%next dev%%'" delete 2>nul
echo Done. (If processes still running, open Task Manager and kill manually.)
