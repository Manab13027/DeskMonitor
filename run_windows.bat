@echo off
REM DeskMonitor launcher for Windows. Double-click to run.
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  echo [deskmonitor] first run: creating .venv and installing psutil...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
)

start "" ".venv\Scripts\pythonw.exe" "%~dp0deskmonitor.py"
