@echo off
rem ---------------------------------------------------------------
rem  STOCRM-AGENT: bot launcher (ASCII only - no encoding issues)
rem  Russian text comes from the Python program, not from this file.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

rem Keep the window open even if the bot stops or crashes.
if not "%~1"=="__keep__" (
    cmd /k ""%~f0" __keep__"
    exit /b
)

title STOCRM-agent (Telegram bot)
cls

if not exist "setup_wizard.py" goto :nofiles
if not exist ".venv\Scripts\python.exe" goto :needsetup
if not exist ".env" goto :needsetup

echo Starting the bot. Do not close this window.
echo Stop: Ctrl+C
echo.
".venv\Scripts\python.exe" -m stocrm_agent.bot
echo.
echo Bot stopped.
goto :done

:needsetup
echo First run: setup is not finished yet.
echo Starting the setup wizard...
echo.
python "setup_wizard.py"
echo.
echo Now run start_bot.bat again.
goto :done

:nofiles
echo [ERROR] setup_wizard.py not found in this folder.
echo Unpack the ZIP archive first, then run setup.bat from that folder.
goto :done

:done
echo.
echo Window will stay open. Press any key to close.
pause >nul
