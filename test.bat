@echo off
REM ChargeSight - run the automated test suite (Windows). Run run.bat once first to create .venv.
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python -m pytest
pause
