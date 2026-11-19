@echo off
rem Windows launcher. Creates .venv and installs libraries when needed.
cd /d "%~dp0"
set "VIRTUAL_ENV="
set "VPY=.venv\Scripts\python.exe"

if exist "%VPY%" goto check

rem Needs a normal Windows Python (python.org). MSYS2/MinGW python makes a broken .venv.
set "PY="
py -3 -c "import sysconfig;raise SystemExit(not sysconfig.get_platform().startswith('win'))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sysconfig;raise SystemExit(not sysconfig.get_platform().startswith('win'))" >nul 2>nul && set "PY=python"
if not defined PY goto nopython

if exist .venv (
    echo Removing broken .venv ...
    rmdir /s /q .venv
)
echo Creating .venv with %PY% ...
%PY% -m venv .venv
if errorlevel 1 goto fail
if not exist "%VPY%" goto fail

:check
"%VPY%" -c "import numpy, serial, sounddevice, pydub" >nul 2>nul
if errorlevel 1 (
    echo Installing libraries, this takes a minute ...
    "%VPY%" -m pip install -r requirements.txt
    if errorlevel 1 goto fail
)

"%VPY%" summary.py %*
pause
exit /b 0

:nopython
echo.
echo Python from python.org was not found.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
echo MSYS2 / MinGW python does not work here.
pause
exit /b 1

:fail
echo.
echo Setup failed. Close this window, delete the .venv folder and run this file again.
echo If it fails again, send the text above.
pause
exit /b 1
