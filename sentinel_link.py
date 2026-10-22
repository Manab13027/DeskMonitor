"""Bridge between DeskMonitor and your Sentinel app (sentinel-desktop).

Sentinel's engine listens on a random 127.0.0.1 port with a per-launch token,
so DeskMonitor gets its status in one of two ways:

1. **Live** - Sentinel (with the small DeskMonitor patch in electron/main.js)
   writes `engine.json` = {port, token, pid} into its data folder. DeskMonitor
   reads it and calls the read-only `/api/summary` endpoint: security score,
   exposed ports, flagged connections, failed checks and recent alerts,
   including Egress Watch upload alerts.
2. **Self-scan** - if Sentinel isn't running (or is an older build without the
   patch), DeskMonitor imports Sentinel's own Python engine from
   `sentinel-desktop/backend` and runs the same read-only scan
   (ports + connections + OS/app audit) every few minutes. It uses a private
   copy of Sentinel's policy so it never writes to Sentinel's database.

DeskMonitor never calls anything that changes state (no block/kill/fix).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any, Optional

import psutil

SYSTEM = platform.system()
SEVERITIES = ["critical", "high", "medium", "low"]


def sentinel_data_dir() -> str:
    if os.environ.get("SENTINEL_DATA"):
        return os.environ["SENTINEL_DATA"]
    if SYSTEM == "Windows":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Sentinel")
    if SYSTEM == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Sentinel")
    return os.path.expanduser("~/.sentinel")


PRIVATE_DATA = os.path.join(os.path.expanduser("~"), ".deskmonitor", "sentinel-selfscan")


class SentinelLink:
    def __init__(self, sentinel_dir: str, scan_every_s: int = 300):
        self.sentinel_dir = os.path.abspath(os.path.expanduser(sentinel_dir))
        self.backend_dir = os.path.join(self.sentinel_dir, "backend")
        self.data_dir = sentinel_data_dir()
        self.scan_every_s = scan_every_s
        self._lock = threading.Lock()
        self._last_scan = 0.0
        self._scanning = False
        self.status: dict[str, Any] = {"state": "checking"}

    # -- discovery --------------------------------------------------------------
    @property
    def engine_available(self) -> bool:
        return os.path.isfile(os.path.join(self.backend_dir, "sentinel", "ports.py"))

    def app_running(self) -> bool:
        for p in psutil.process_iter(["name", "exe"]):
            try:
                n = (p.info.get("name") or "").lower()
                exe = (p.info.get("exe") or "").lower()
                if n in ("sentinel", "sentinel.exe", "sentinel-backend", "sentinel-backend.exe") \
                        or "sentinel.app/contents/macos" in exe:
                    return True
            except Exception:
                continue
        return False

    def _engine_info(self) -> Optional[dict]:
        try:
            with open(os.path.join(self.data_dir, "engine.json"), encoding="utf-8") as fh:
                info = json.load(fh)
            if info.get("pid") and not psutil.pid_exists(int(info["pid"])):
                return None   # stale file from a previous run
            return info
        except Exception:
            return None

    def _api(self, info: dict, path: str, timeout: float = 4) -> Optional[dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}{path}",
                                     headers={"X-Sentinel-Token": info["token"]})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    # -- polling (worker thread) -------------------------------------------------
    def poll(self) -> dict[str, Any]:
        running = self.app_running()
        info = self._engine_info()
        if info:
            summ = self._api(info, "/api/summary")
            if summ is not None:
                self._set(self._from_summary(summ, live=True))
                return self.status

        # no live engine: embedded self-scan on a slow cadence
        if not self.engine_available:
            self._set({"state": "missing", "app_running": running,
                       "detail": f"Sentinel not found at {self.sentinel_dir}"})
            return self.status
        if time.time() - self._last_scan >= self.scan_every_s and not self._scanning:
            self._self_scan(running)
        else:
            with self._lock:
                self.status = {**self.status, "app_running": running}
        return self.status

    def scan_now(self) -> None:
        self._last_scan = 0
        threading.Thread(target=self.poll, daemon=True).start()

    def _from_summary(self, s: dict, live: bool) -> dict:
        sc = s.get("score", {})
        ports, conns, vulns = s.get("ports", {}), s.get("connections", {}), s.get("vulns", {})
        bv = conns.get("by_verdict", {})
        alerts = []
        for a in (s.get("alerts") or [])[:3]:
            alerts.append({"severity": str(a.get("severity", "medium")).lower(), "title": a.get("title", "")})
        if not alerts:   # fall back to the top of the action plan
            for p in (s.get("plan") or [])[:2]:
                alerts.append({"severity": str(p.get("severity", "medium")).lower(),
                               "title": p.get("title") or p.get("action") or ""})
        return {
            "state": "live" if live else "selfscan",
            "app_running": True if live else self.app_running(),
            "score": sc.get("score"), "grade": sc.get("grade"),
            "by_severity": sc.get("by_severity", {}),
            "ports_exposed": ports.get("exposed", 0), "ports_close": ports.get("close", 0),
            "conn_flagged": sum(bv.get(k, 0) for k in ("malicious", "unauthorised", "suspicious")),
            "conn_malicious": bv.get("malicious", 0), "conn_total": conns.get("total", 0),
            "vuln_fail": vulns.get("fail", 0),
            "alerts": [a for a in alerts if a["title"]],
            "learning": (conns.get("meta") or {}).get("learning"),
            "events_24h": self._events_24h(),
            "updated": time.time(),
        }

    def _events_24h(self) -> Optional[int]:
        """High/critical events Sentinel logged in the last 24h (read-only)."""
        db = os.path.join(self.data_dir, "sentinel.db")
        if not os.path.exists(db):
            return None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            n = con.execute("SELECT COUNT(*) FROM events WHERE ts > ? AND severity IN ('critical','high')",
                            (time.time() - 86400,)).fetchone()[0]
            con.close()
            return int(n)
        except Exception:
            return None

    def _prepare_private_data(self) -> None:
        """Self-scan uses its own data dir so it never touches Sentinel's DB, but
        copies your policy (allow/block lists, trusted apps) and imported feeds."""
        os.makedirs(PRIVATE_DATA, exist_ok=True)
        for name in ("policy.json", "cve_extra.json"):
            src = os.path.join(self.data_dir, name)
            if os.path.exists(src):
                try:
                    shutil.copy2(src, os.path.join(PRIVATE_DATA, name))
                except Exception:
                    pass
        feeds = os.path.join(self.data_dir, "feeds")
        if os.path.isdir(feeds):
            try:
                shutil.copytree(feeds, os.path.join(PRIVATE_DATA, "feeds"), dirs_exist_ok=True)
            except Exception:
                pass

    def _self_scan(self, running: bool) -> None:
        self._scanning = True
        with self._lock:
            self.status = {**self.status, "state": "scanning", "app_running": running}
        try:
            self._prepare_private_data()
            os.environ["SENTINEL_DATA"] = PRIVATE_DATA
            if self.backend_dir not in sys.path:
                sys.path.insert(0, self.backend_dir)
            from sentinel import advisor, network, ports, vulns   # type: ignore

            pr = ports.scan_ports()
            cr = network.scan_connections(resolve=False)
            open_ports = {p["port"] for p in pr["ports"] if p["exposure"] != "loopback"}
            ar = vulns.audit(deep=False, open_ports=open_ports)
            st = {"ports": pr["ports"], "connections": cr["connections"], "findings": ar["findings"]}
            sc = advisor.score(st)
            plan = advisor.plan(st, 3)
            pc, cc = pr["ports"], cr["connections"]
            fails = [f for f in ar["findings"] if f["status"] == "fail"]
            summ = {
                "score": sc,
                "plan": plan,
                "ports": {"exposed": sum(p["exposure"] != "loopback" for p in pc),
                          "close": sum(p["recommendation"] == "close" and p["exposure"] != "loopback" for p in pc)},
                "connections": {"total": len(cc),
                                "by_verdict": {v: sum(c["verdict"] == v for c in cc)
                                               for v in ("malicious", "unauthorised", "suspicious")},
                                "meta": {"learning": cr.get("learning")}},
                "vulns": {"fail": len(fails)},
                "alerts": [{"severity": c["severity"], "title": f"{c['app']} → {c['remote_ip']}:{c['remote_port']}"}
                           for c in cc if c["verdict"] in ("malicious", "unauthorised", "suspicious")][:3],
            }
            self._set(self._from_summary(summ, live=False))
        except Exception as exc:
            self._set({"state": "error", "app_running": running, "detail": f"self-scan failed: {exc}"})
        finally:
            self._last_scan = time.time()
            self._scanning = False

    def _set(self, st: dict) -> None:
        with self._lock:
            self.status = st

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.status)

    # -- actions -------------------------------------------------------------------
    def open_app(self) -> bool:
        """Open Sentinel's window (it's single-instance, so this just shows it)."""
        try:
            if SYSTEM == "Darwin":
                r = subprocess.run(["open", "-a", "Sentinel"], capture_output=True)
                return r.returncode == 0
            if SYSTEM == "Windows":
                for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
                    exe = os.path.join(base, "Programs" if "Local" in base else "", "Sentinel", "Sentinel.exe")
                    if os.path.exists(exe):
                        os.startfile(exe)   # type: ignore[attr-defined]
                        return True
        except Exception:
            pass
        return False
