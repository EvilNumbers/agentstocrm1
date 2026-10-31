@echo off
rem ---------------------------------------------------------------
rem  STOCRM-AGENT: setup launcher (ASCII only - no encoding issues)
rem  All Russian text is printed by setup_wizard.py, not by this file.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

rem Re-launch inside "cmd /k" so the window never closes by itself.
if not "%~1"=="__keep__" (
    cmd /k ""%~f0" __keep__"
    exit /b
)

title STOCRM-agent setup
cls
echo ==============================================================
echo   STOCRM-AGENT / INSTALLATION
echo ==============================================================
echo.

if not exist "setup_wizard.py" goto :nofiles

set "PY="
where py >nul 2>&1
if not errorlevel 1 set "PY=py"
if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY goto :nopython

%PY% "setup_wizard.py"
set "RC=%ERRORLEVEL%"
goto :done

:nofiles
echo [ERROR] setup_wizard.py not found in this folder.
echo Unpack the ZIP archive into a folder first (right click - Extract All),
echo then run setup.bat from that folder.
goto :done

:nopython
echo [ERROR] Python not found.
echo 1. Open https://www.python.org/downloads/
echo 2. Run the installer and CHECK "Add python.exe to PATH"
echo 3. Run setup.bat again
goto :done

:done
echo.
echo Window will stay open. Press any key to close.
pause >nul
