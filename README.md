# DeskMonitor

A small system-diagnostics widget that sits on your desktop (behind your
windows, like part of the wallpaper) on **macOS** and **Windows**.

It shows:

- **CPU** usage, cores and load
- **Temperature** (°C where the OS allows it, otherwise the thermal state)
- **Memory** and swap
- **Disk space** per drive
- **Battery** level and time left (laptops)
- **Network** up/down speed
- **Top running apps** by CPU, with memory (helper processes grouped, e.g. all of Chrome counts once)
- **Sentinel** status: security score and grade, exposed ports, flagged
  connections, failed checks by severity and the latest alerts (including
  Egress Watch upload alerts), live from your running Sentinel app

## Run

**macOS**: double-click `run_mac.command` (or `./run_mac.command` in Terminal).
**Windows**: double-click `run_windows.bat`.

The first run creates a `.venv` and installs `psutil` (plus `pyobjc` on macOS).
Requires Python 3.9+ with Tkinter. On macOS with Homebrew Python, run
`brew install python-tk` first; the python.org installer already includes it.

Start at login:

```bash
.venv/bin/python install_autostart.py           # macOS
.venv\Scripts\python install_autostart.py       # Windows
# add --remove to undo
```

## Using it

- **Drag** it anywhere; the position is remembered.
- **Click the Sentinel section** to open the Sentinel window (or launch it).
- **Right-click** (or Ctrl+click) for: refresh, open Sentinel, run a
  Sentinel self-scan now, pin mode (desktop / always on top / normal),
  opacity, reset position, quit.

## Sentinel integration

DeskMonitor looks for Sentinel in `../sentinel-desktop` (next to this folder).
Change `sentinel_dir` in `config.json` if it lives elsewhere.

| Sentinel state | What the widget shows |
|---|---|
| Sentinel running (with the link patch) | **LIVE**: score, ports, connections, checks and alerts from Sentinel's `/api/summary` |
| Sentinel not running, or an older build | **SELF-SCAN**: imports Sentinel's own engine from `backend/` and runs its read-only scan (ports + connections + OS/app audit) every 5 min |
| Folder not found | **NOT FOUND** with the path it checked |

**The link patch.** Sentinel's engine uses a random port and a per-launch token
that only the Electron app knows. A small addition to `electron/main.js`
(`publishEngine()`) writes `{port, token, pid}` to `engine.json` in Sentinel's
data folder (owner-only permissions) and deletes it when Sentinel quits.
Rebuild Sentinel once to pick it up: `bash scripts/install_mac.sh` on the Mac,
`npm run dist:win` on Windows.

The widget only calls `/api/summary`. It never blocks, kills or fixes anything.
The self-scan uses a private copy of your Sentinel policy (allow/block lists,
trusted apps, imported feeds) in `~/.deskmonitor/sentinel-selfscan`, so it
never writes to Sentinel's own database. It also reads (read-only) how many
high/critical events Sentinel logged in the last 24 h.

## Temperature notes

- **macOS**: Apple doesn't expose CPU temperature without admin rights. The
  widget shows the system **thermal state** (Nominal / Fair / Serious /
  Critical). For real °C, install a free sensor tool and DeskMonitor picks it
  up automatically: `brew install narugit/tap/smctemp` (Apple Silicon and
  Intel). `osx-cpu-temp` and `istats` also work.
- **Windows**: Windows has no built-in CPU temperature API. Run
  [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
  in the background and DeskMonitor reads its sensors. Otherwise it tries the
  ACPI thermal zone, which often needs admin.

## Config (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `sentinel_dir` | `../sentinel-desktop` | Path to your Sentinel project |
| `sentinel_scan_every_s` | `300` | Self-scan interval when Sentinel isn't running |
| `refresh_s` | `2` | System metrics refresh interval |
| `width` | `300` | Widget width in px |
| `top_apps` | `6` | Rows in the Top apps list |

Position, pin mode and opacity are saved in `~/.deskmonitor.json`.

## Files

```
deskmonitor.py       widget UI, desktop pinning, menu
collectors.py        CPU / temp / memory / disk / battery / network / apps
sentinel_link.py     Sentinel live API + embedded self-scan
install_autostart.py start at login (LaunchAgent / Startup folder)
run_mac.command      macOS launcher
run_windows.bat      Windows launcher
```

`python3 deskmonitor.py --once` prints one snapshot as JSON (handy for debugging).
