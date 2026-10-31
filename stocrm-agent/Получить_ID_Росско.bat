@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" goto :needsetup
".venv\Scripts\python.exe" -m stocrm_agent.rossko_delivery
goto :end
:needsetup
echo Run setup.bat first, then try again.
:end
echo.
echo Press any key to close.
pause >nul
