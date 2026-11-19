@echo off
rem Windows launcher. First run installs libraries into .venv
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)
.venv\Scripts\python.exe summary.py %*
pause
