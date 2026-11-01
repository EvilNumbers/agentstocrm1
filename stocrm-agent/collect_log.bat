@echo off
rem Local diagnostic text log. No CRM requests and no automatic sending.
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
title STOCRM-agent: diagnostic log
cls
"%PY%" -m stocrm_agent.cli support-log --out support_log.txt
if errorlevel 1 goto :failed
echo.
echo The file support_log.txt is in this folder. Review it before sending.
start "" notepad "%~dp0support_log.txt"
goto :done
:failed
echo.
echo [ERROR] The file was not created. Please check Python and disk space.
:done
echo.
pause
