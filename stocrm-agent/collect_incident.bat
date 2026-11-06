@echo off
rem ---------------------------------------------------------------
rem  STOCRM-AGENT: short incident digest (ASCII only)
rem  Collects only the last 45 minutes of the local journal.
rem  Optional: collect_incident.bat --offer 1371 --minutes 20
rem  Result: incident.txt next to this file - small, send this one.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

title STOCRM-agent: incident digest
cls
echo Collecting the incident digest. Local only, no CRM requests...
echo.

"%PY%" -m stocrm_agent.cli support-incident --minutes 45 --out incident.txt %*
if errorlevel 1 goto :failed

echo.
echo Opening the file in Notepad. Check it before sending.
start "" notepad "%~dp0incident.txt"
goto :done

:failed
echo.
echo [ERROR] The digest was not created.
echo Run setup.bat first (Python environment), then try again.
echo.

:done
echo Press any key to close.
pause >nul
