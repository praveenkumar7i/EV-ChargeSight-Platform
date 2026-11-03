@echo off
REM ChargeSight - Windows start script. First run creates a virtual environment and installs dependencies.
cd /d "%~dp0"
if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv || goto :nopython
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt || goto :fail
)
if not exist .env copy .env.example .env >nul
.venv\Scripts\python run.py
goto :eof

:nopython
echo Python 3.10+ was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
pause
goto :eof

:fail
echo Dependency installation failed. Check your internet connection and try again.
pause
