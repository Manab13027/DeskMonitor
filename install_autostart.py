#!/usr/bin/env python3
"""Make DeskMonitor start automatically when you log in.

    python3 install_autostart.py            # install
    python3 install_autostart.py --remove   # uninstall

Run it with the same Python that runs the widget (the launchers create
.venv for you - e.g. `.venv/bin/python install_autostart.py`).
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "deskmonitor.py")
LABEL = "com.deskmonitor.widget"


def _python(gui: bool) -> str:
    if os.name == "nt":
        venv = os.path.join(HERE, ".venv", "Scripts", "pythonw.exe" if gui else "python.exe")
        if os.path.exists(venv):
            return venv
        exe = sys.executable
        alt = exe.replace("python.exe", "pythonw.exe")
        return alt if gui and os.path.exists(alt) else exe
    venv = os.path.join(HERE, ".venv", "bin", "python")
    return venv if os.path.exists(venv) else sys.executable


def mac(remove: bool) -> None:
    plist = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
    subprocess.run(["launchctl", "unload", plist], capture_output=True)
    if remove:
        if os.path.exists(plist):
            os.remove(plist)
        print("removed", plist)
        return
    os.makedirs(os.path.dirname(plist), exist_ok=True)
    with open(plist, "w", encoding="utf-8") as fh:
        fh.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{_python(True)}</string><string>{SCRIPT}</string></array>
  <key>WorkingDirectory</key><string>{HERE}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>/tmp/deskmonitor.log</string>
  <key>StandardErrorPath</key><string>/tmp/deskmonitor.log</string>
</dict>
</plist>
""")
    subprocess.run(["launchctl", "load", plist])
    print("installed", plist, "- DeskMonitor will start at login (and is starting now).")


def windows(remove: bool) -> None:
    startup = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")
    vbs = os.path.join(startup, "DeskMonitor.vbs")
    if remove:
        if os.path.exists(vbs):
            os.remove(vbs)
        print("removed", vbs)
        return
    with open(vbs, "w", encoding="utf-8") as fh:
        fh.write('Set sh = CreateObject("WScript.Shell")\r\n'
                 f'sh.CurrentDirectory = "{HERE}"\r\n'
                 f'sh.Run """{_python(True)}"" ""{SCRIPT}""", 0, False\r\n')
    print("installed", vbs, "- DeskMonitor will start at login.")


if __name__ == "__main__":
    remove = "--remove" in sys.argv
    sysname = platform.system()
    if sysname == "Darwin":
        mac(remove)
    elif sysname == "Windows":
        windows(remove)
    else:
        print("Autostart helper supports macOS and Windows. On Linux, add "
              f"`{_python(True)} {SCRIPT}` to your desktop's startup applications.")
