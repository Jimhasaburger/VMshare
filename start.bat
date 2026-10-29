@echo off
echo === VMShare - VM Agent ===
echo.
echo This runs on each machine that hosts VMs.
echo Edit config.json to set machine name and VM list.
echo.
cd /d "%~dp0"
py -m pip install -r requirements.txt
echo.
echo Starting VM Agent...
py agent.py
pause
