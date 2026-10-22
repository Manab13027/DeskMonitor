"""Bridge between DeskMonitor and your Defender app.

Two ways of getting Defender's status, tried in order:

1. **Live** - if Defender's web server is running (default http://localhost:5050),
   read /api/stats. Read-only; it never calls /api/scan or /api/reset, so it
   won't wipe the history you're looking at in the dashboard.
2. **Embedded scan** - if the server isn't running, import the Defender package
   straight from its folder and run the same read-only self-scan its
   "SCAN THIS MACHINE" button does (ports, posture, processes, connections).
   Runs on a slow timer in a background thread.

Nothing here changes system state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any, Optional

SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]


class DefenderLink:
    def __init__(self, defender_dir: str, port: int = 5050, scan_every_s: int = 300):
        self.defender_dir = os.path.abspath(os.path.expanduser(defender_dir))
        self.port = port
        self.url = f"http://localhost:{port}"
        self.scan_every_s = scan_every_s
        self._lock = threading.Lock()
        self._last_scan = 0.0
        self._scanning = False
        self.status: dict[str, Any] = {"state": "checking", "counts": {}, "top": [], "source": ""}

    # -- availability -------------------------------------------------------
    @property
    def installed(self) -> bool:
        return os.path.isfile(os.path.join(self.defender_dir, "defender", "engine.py"))

    def _get(self, path: str, timeout: float = 1.5) -> Optional[dict]:
        try:
            with urllib.request.urlopen(self.url + path, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    # -- polling (call from a worker thread) ---------------------------------
    def poll(self) -> dict[str, Any]:
        if not self.installed:
            self._set({"state": "missing", "counts": {}, "top": [], "source": "",
                       "detail": f"Defender not found at {self.defender_dir}"})
            return self.status

        stats = self._get("/api/stats")
        if stats is not None:
            hist = self._get("/api/history?limit=200") or {}
            alerts = sorted(hist.get("alerts", []),
                            key=lambda a: (a.get("severity_level", 0), a.get("score", 0)), reverse=True)
            top = [{"severity": a.get("severity", "?"), "title": a.get("title", "")} for a in alerts[:3]]
            self._set({"state": "online", "source": "live",
                       "counts": stats.get("by_severity", {}), "total": stats.get("total", 0),
                       "top": top, "updated": time.time()})
            return self.status

        # Server offline -> embedded scan on a slow cadence
        if time.time() - self._last_scan >= self.scan_every_s and not self._scanning:
            self._embedded_scan()
        else:
            with self._lock:
                if self.status.get("state") in ("online", "checking"):
                    self.status = {**self.status, "state": "offline"}
        return self.status

    def scan_now(self) -> None:
        self._last_scan = 0
        threading.Thread(target=self.poll, daemon=True).start()

    def _embedded_scan(self) -> None:
        self._scanning = True
        with self._lock:
            self.status = {**self.status, "state": "scanning"}
        try:
            if self.defender_dir not in sys.path:
                sys.path.insert(0, self.defender_dir)
            from defender.engine import Engine            # type: ignore
            from defender.detectors import ALL_DETECTORS  # type: ignore
            from defender import local_scan               # type: ignore

            engine = Engine()
            for cls in ALL_DETECTORS:
                engine.register(cls())
            s = local_scan.scan()
            alerts = []
            for kind, key in (("ports", "ports_payload"), ("vuln", "vuln_payload"),
                              ("endpoint", "endpoint_payload"), ("network", "network_payload")):
                try:
                    alerts += engine.analyze(kind, s[key])
                except Exception:
                    pass
            alerts.sort(key=lambda a: (a.severity, a.score), reverse=True)
            counts = {sev: 0 for sev in SEVERITIES}
            for a in alerts:
                counts[a.severity.label] = counts.get(a.severity.label, 0) + 1
            self._set({"state": "offline", "source": "self-scan", "counts": counts,
                       "total": len(alerts),
                       "top": [{"severity": a.severity.label, "title": a.title} for a in alerts[:3]],
                       "updated": time.time()})
        except Exception as exc:
            self._set({"state": "error", "counts": {}, "top": [], "source": "",
                       "detail": f"scan failed: {exc}"})
        finally:
            self._last_scan = time.time()
            self._scanning = False

    def _set(self, st: dict[str, Any]) -> None:
        with self._lock:
            self.status = st

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.status)

    # -- actions --------------------------------------------------------------
    def launch_server(self) -> bool:
        """Start Defender's own app.py in the background (no browser pop-up)."""
        if not self.installed:
            return False
        env = {**os.environ, "PORT": str(self.port), "DEFENDER_NO_BROWSER": "1"}
        kw: dict[str, Any] = {"cwd": self.defender_dir, "env": env,
                              "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000 | 0x00000200   # no window, new group
        else:
            kw["start_new_session"] = True
        try:
            subprocess.Popen([sys.executable, "app.py"], **kw)
            return True
        except Exception:
            return False
