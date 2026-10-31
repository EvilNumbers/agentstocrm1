@echo off
rem ---------------------------------------------------------------
rem  STOCRM-AGENT: collect a support report (ASCII only - no encoding issues)
rem  Russian text comes from the Python program, not from this file.
rem  Result: support_report.txt next to this file - send it in the chat.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

title STOCRM-agent: support report
cls
echo Collecting the report. Extended local report (no CRM changes). Please wait...
echo.

"%PY%" -m stocrm_agent.cli support --lines 1500
if errorlevel 1 goto :failed

echo.
echo Opening the file in Notepad.
echo Fill in section 2 (what you sent, what the bot answered) and save it.
start "" notepad "support_report.txt"
goto :done

:failed
echo.
echo [ERROR] The report was not created.
echo Run setup.bat first (Python environment), then try again.
echo.

:done
echo.
echo Press any key to close.
pause >nul
