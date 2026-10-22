"""System metric collectors for DeskMonitor (macOS + Windows, works on Linux too).

Everything here is read-only. Each collector is wrapped so a missing tool or a
permission error degrades to "n/a" instead of crashing the widget.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from typing import Any, Optional

import psutil

SYSTEM = platform.system()          # "Darwin" | "Windows" | "Linux"
IS_MAC = SYSTEM == "Darwin"
IS_WIN = SYSTEM == "Windows"

_NO_WINDOW = 0x08000000 if IS_WIN else 0   # CREATE_NO_WINDOW: no console flash


def _run(cmd: list[str], timeout: float = 4) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             creationflags=_NO_WINDOW)
        return (out.stdout or "").strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Temperature
# --------------------------------------------------------------------------

def _temp_psutil() -> Optional[float]:
    fn = getattr(psutil, "sensors_temperatures", None)   # Linux/FreeBSD only
    if not fn:
        return None
    try:
        temps = fn()
    except Exception:
        return None
    best = None
    for name in ("coretemp", "k10temp", "cpu_thermal", "acpitz", "zenpower"):
        for t in temps.get(name, []):
            if t.current and (best is None or t.current > best):
                best = t.current
    if best is None:
        for entries in temps.values():
            for t in entries:
                if t.current and (best is None or t.current > best):
                    best = t.current
    return best


def _temp_mac() -> tuple[Optional[float], str]:
    """Apple Silicon/Intel Macs don't expose CPU temperature to normal users.
    Try common free CLI tools if installed, then fall back to the OS thermal
    pressure level (always available, no sudo)."""
    for tool, args in (("smctemp", ["-c"]), ("osx-cpu-temp", []), ("istats", ["cpu", "temp", "--value-only"])):
        path = shutil.which(tool) or (f"/opt/homebrew/bin/{tool}" if os.path.exists(f"/opt/homebrew/bin/{tool}") else None)
        if path:
            m = re.search(r"(\d+(?:\.\d+)?)", _run([path, *args]))
            if m and 0 < float(m.group(1)) < 130:
                return float(m.group(1)), ""
    return None, _thermal_state_mac()


def _thermal_state_mac() -> str:
    try:  # pyobjc gives the same value Activity Monitor uses
        from Foundation import NSProcessInfo
        return ["Nominal", "Fair", "Serious", "Critical"][int(NSProcessInfo.processInfo().thermalState())]
    except Exception:
        pass
    out = _run(["pmset", "-g", "therm"]).lower()
    if "cpu_speed_limit" in out:
        m = re.search(r"cpu_speed_limit\s*=\s*(\d+)", out)
        if m and int(m.group(1)) < 100:
            return f"Throttled {m.group(1)}%"
    return "Nominal" if out else ""


_win_temp_cache: tuple[float, Optional[float]] = (0.0, None)


def _temp_windows() -> Optional[float]:
    """Try LibreHardwareMonitor/OpenHardwareMonitor WMI (if running), then the
    ACPI thermal zone (often needs admin). Cached for 30s - PowerShell is slow."""
    global _win_temp_cache
    if time.time() - _win_temp_cache[0] < 30:
        return _win_temp_cache[1]
    ps = (
        "$v=$null;"
        "foreach($ns in 'root/LibreHardwareMonitor','root/OpenHardwareMonitor'){"
        " try{$s=Get-CimInstance -Namespace $ns -ClassName Sensor -EA Stop|"
        "  ?{$_.SensorType -eq 'Temperature' -and $_.Name -match 'CPU|Package|Core'}|"
        "  Measure-Object -Property Value -Maximum; if($s.Maximum){$v=$s.Maximum;break}}catch{}};"
        "if(-not $v){try{$t=Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -EA Stop|"
        " Measure-Object -Property CurrentTemperature -Maximum; $v=($t.Maximum/10)-273.15}catch{}};"
        "if($v){[math]::Round($v,1)}"
    )
    out = _run(["powershell", "-NoProfile", "-Command", ps], timeout=8)
    val = None
    try:
        v = float(out.splitlines()[-1]) if out else None
        val = v if v is not None and 0 < v < 130 else None
    except Exception:
        pass
    _win_temp_cache = (time.time(), val)
    return val


def temperature() -> dict[str, Any]:
    if IS_MAC:
        t, state = _temp_mac()
        return {"cpu_c": t, "state": state,
                "hint": "" if t is not None else "brew install smctemp for °C"}
    if IS_WIN:
        t = _temp_windows()
        return {"cpu_c": t, "state": "",
                "hint": "" if t is not None else "run LibreHardwareMonitor for °C"}
    t = _temp_psutil()
    return {"cpu_c": t, "state": "", "hint": "" if t is not None else "no sensor"}


# --------------------------------------------------------------------------
# CPU / memory / disk / battery / network
# --------------------------------------------------------------------------

def cpu() -> dict[str, Any]:
    try:
        load = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
    except OSError:
        load = None
    freq = None
    try:
        f = psutil.cpu_freq()
        freq = f.current if f and f.current else None
    except Exception:
        pass
    return {"percent": psutil.cpu_percent(interval=None),
            "cores": psutil.cpu_count(logical=True), "load1": load, "mhz": freq}


def memory() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent,
            "swap_used": sw.used, "swap_percent": sw.percent}


_SKIP_FS = {"devfs", "autofs", "tmpfs", "devtmpfs", "squashfs", "overlay", "nullfs", "proc"}


def disks() -> list[dict[str, Any]]:
    out, seen = [], set()
    for p in psutil.disk_partitions(all=False):
        mp = p.mountpoint
        if p.fstype.lower() in _SKIP_FS:
            continue
        if IS_MAC and mp != "/" and not mp.startswith("/Volumes/"):
            continue                       # hide APFS system/VM/preboot volumes
        if IS_WIN and "cdrom" in p.opts.lower():
            continue
        if not IS_MAC and not IS_WIN and mp.startswith(("/snap", "/boot", "/run", "/sys")):
            continue
        try:
            u = psutil.disk_usage(mp)
        except Exception:
            continue
        if u.total < 1 << 30 or (u.total, u.used) in seen:   # skip tiny / duplicate
            continue
        seen.add((u.total, u.used))
        label = "Macintosh HD" if IS_MAC and mp == "/" else (mp.rstrip("\\") if IS_WIN else os.path.basename(mp) or mp)
        out.append({"label": label, "total": u.total, "used": u.used,
                    "free": u.free, "percent": u.percent})
    return out[:4]


def battery() -> Optional[dict[str, Any]]:
    try:
        b = psutil.sensors_battery()
    except Exception:
        return None
    if not b:
        return None
    return {"percent": b.percent, "plugged": b.power_plugged,
            "secs_left": b.secsleft if b.secsleft not in (psutil.POWER_TIME_UNLIMITED,
                                                          psutil.POWER_TIME_UNKNOWN) else None}


_last_net: tuple[float, int, int] | None = None


def network() -> dict[str, Any]:
    global _last_net
    c = psutil.net_io_counters()
    now = time.time()
    up = down = 0.0
    if _last_net:
        dt = max(now - _last_net[0], 1e-3)
        up = (c.bytes_sent - _last_net[1]) / dt
        down = (c.bytes_recv - _last_net[2]) / dt
    _last_net = (now, c.bytes_sent, c.bytes_recv)
    return {"up_bps": max(up, 0), "down_bps": max(down, 0)}


def uptime_s() -> float:
    return time.time() - psutil.boot_time()


# --------------------------------------------------------------------------
# Running applications
# --------------------------------------------------------------------------

_procs: dict[int, psutil.Process] = {}
_IDLE = {"System Idle Process", "idle", "kernel_task", "System", "Registry", "MemCompression"}


def _app_name(p: psutil.Process) -> str:
    name = p.info.get("name") or "?"
    if IS_MAC:
        # "/Applications/Google Chrome.app/Contents/..." -> "Google Chrome"
        exe = p.info.get("exe") or ""
        m = re.search(r"/([^/]+)\.app/", exe)
        if m:
            return m.group(1)
    if IS_WIN and name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def applications(top: int = 6) -> dict[str, Any]:
    """Top apps by CPU, grouped by app name (Chrome's 30 helpers count as one).
    The first call primes per-process CPU counters, so values settle after
    one refresh."""
    global _procs
    grouped: dict[str, dict[str, float]] = {}
    alive: dict[int, psutil.Process] = {}
    for p in psutil.process_iter(["pid", "name", "exe", "memory_info"]):
        try:
            proc = _procs.get(p.pid) or p
            alive[p.pid] = proc
            proc.info = p.info
            name = _app_name(p)
            if name in _IDLE or not p.info.get("memory_info") or p.info["memory_info"].rss == 0:
                continue   # skip idle/kernel threads
            c = proc.cpu_percent(None)
            rss = (p.info.get("memory_info").rss if p.info.get("memory_info") else 0)
            g = grouped.setdefault(name, {"cpu": 0.0, "mem": 0, "count": 0})
            g["cpu"] += c
            g["mem"] += rss
            g["count"] += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
    _procs = alive
    ncpu = psutil.cpu_count() or 1
    rows = [{"name": n, "cpu": g["cpu"] / ncpu, "mem": g["mem"], "count": int(g["count"])}
            for n, g in grouped.items()]
    rows.sort(key=lambda r: (r["cpu"], r["mem"]), reverse=True)
    return {"top": rows[:top], "process_count": len(alive), "app_count": len(grouped)}


# --------------------------------------------------------------------------

def snapshot() -> dict[str, Any]:
    return {
        "host": platform.node().split(".")[0] or "this machine",
        "os": f"{'macOS' if IS_MAC else SYSTEM} {platform.mac_ver()[0] if IS_MAC else platform.release()}",
        "uptime": uptime_s(),
        "cpu": cpu(),
        "memory": memory(),
        "disks": disks(),
        "battery": battery(),
        "network": network(),
        "apps": applications(),
        "ts": time.time(),
    }
