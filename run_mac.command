#!/bin/bash
# DeskMonitor launcher for macOS. Double-click in Finder, or run ./run_mac.command
set -e
cd "$(dirname "$0")"

PY=python3
if ! $PY -c "import tkinter" 2>/dev/null; then
  echo "[deskmonitor] Your python3 has no Tkinter."
  echo "  Homebrew:   brew install python-tk"
  echo "  or install Python from https://www.python.org (includes Tk)"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "[deskmonitor] first run: creating .venv and installing psutil + pyobjc…"
  $PY -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
fi

nohup .venv/bin/python deskmonitor.py >/tmp/deskmonitor.log 2>&1 &
disown
echo "[deskmonitor] running (log: /tmp/deskmonitor.log). You can close this window."
