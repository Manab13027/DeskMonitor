#!/usr/bin/env python3
"""DeskMonitor - a small system-diagnostics widget that lives on your desktop.

Shows CPU, temperature, memory, disks, battery, network, the busiest running
apps, and the status of your Sentinel app. Works on macOS and Windows.

    python3 deskmonitor.py            # run it
    python3 deskmonitor.py --once     # print one snapshot as JSON and exit

Drag it anywhere. Right-click (or Ctrl+click) for options.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

import collectors as col
from sentinel_link import SentinelLink, SEVERITIES

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(os.path.expanduser("~"), ".deskmonitor.json")

DEFAULT_CONFIG = {
    "sentinel_dir": "../sentinel-desktop",   # relative to this folder, or absolute
    "sentinel_scan_every_s": 300,    # self-scan cadence when Sentinel isn't running
    "refresh_s": 2,
    "width": 300,
    "opacity": 0.94,
    "pin_mode": "desktop",           # desktop | top | normal
    "top_apps": 6,
}

# ---- palette -------------------------------------------------------------
BG = "#0f1419"
PANEL = "#151b22"
LINE = "#26303b"
TEXT = "#e6edf3"
MUTED = "#8b98a5"
TRACK = "#222b35"
ACCENT = "#39d98a"       # Sentinel green
OK, WARN, BAD = "#39d98a", "#f5b942", "#ff5c5c"
SEV_COLORS = {"critical": "#ff3b6b", "high": "#ff7a45", "medium": "#f5b942",
              "low": "#58a6ff", "info": "#8b98a5"}
KEY = "#010203"          # transparent key colour on Windows


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(HERE, "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except Exception:
        pass
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            cfg.update(json.load(fh))   # remembered position / pin mode / opacity
    except Exception:
        pass
    d = os.path.expanduser(cfg["sentinel_dir"])
    cfg["sentinel_dir"] = d if os.path.isabs(d) else os.path.normpath(os.path.join(HERE, d))
    return cfg


def save_state(**kw) -> None:
    try:
        cur = {}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as fh:
                cur = json.load(fh)
        cur.update(kw)
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(cur, fh)
    except Exception:
        pass


# ---- formatting ------------------------------------------------------------
def fmt_bytes(n: float, digits: int = 1) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.{digits}f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_rate(bps: float) -> str:
    return fmt_bytes(bps) + "/s"


def fmt_uptime(s: float) -> str:
    d, s = divmod(int(s), 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")


def level_color(pct: float, warn: float = 70, bad: float = 90) -> str:
    return BAD if pct >= bad else WARN if pct >= warn else OK


def ago(ts: float | None) -> str:
    if not ts:
        return ""
    s = int(time.time() - ts)
    return "just now" if s < 10 else f"{s}s ago" if s < 60 else f"{s // 60}m ago"


# ---- desktop pinning ---------------------------------------------------------
class Pinner:
    """Keeps the window at desktop level (behind other windows), on top, or
    normal. Uses native APIs when available and degrades gracefully."""

    MAC_DESKTOP_ICON_LEVEL = -2147483603   # kCGDesktopIconWindowLevel

    def __init__(self, root: tk.Tk):
        self.root = root
        self.mode = "normal"
        self._nswin = None
        self._hwnd = None

    def setup(self) -> None:
        self.root.update_idletasks()
        if col.IS_WIN:
            try:
                import ctypes
                u = ctypes.windll.user32
                self._hwnd = u.GetParent(self.root.winfo_id()) or self.root.winfo_id()
                GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
                style = u.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
                u.SetWindowLongW(self._hwnd, GWL_EXSTYLE, (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
            except Exception:
                self._hwnd = None
        elif col.IS_MAC:
            try:
                from AppKit import NSApp   # pyobjc-framework-Cocoa
                NSApp.setActivationPolicy_(1)          # accessory: no Dock icon
                wins = [w for w in NSApp.windows() if w.isVisible()]
                self._nswin = wins[-1] if wins else None
            except Exception:
                self._nswin = None

    def apply(self, mode: str) -> None:
        self.mode = mode
        top = mode == "top"
        try:
            self.root.attributes("-topmost", top)
        except Exception:
            pass
        if self._nswin is not None:
            try:
                # 0 = NSNormalWindowLevel, 3 = NSFloatingWindowLevel
                level = {"desktop": self.MAC_DESKTOP_ICON_LEVEL + 1, "top": 3}.get(mode, 0)
                self._nswin.setLevel_(level)
                # all Spaces + stationary (survives Mission Control / Show Desktop)
                self._nswin.setCollectionBehavior_((1 << 0) | (1 << 4) | (1 << 6) if mode != "normal" else 0)
                self._nswin.setHasShadow_(False)
            except Exception:
                pass
        self.reassert()

    def reassert(self) -> None:
        """Called every refresh: on Windows, push the window back to the bottom
        of the z-order so it behaves like part of the wallpaper."""
        if self.mode == "desktop" and self._hwnd:
            try:
                import ctypes
                HWND_BOTTOM, SWP = 1, 0x0001 | 0x0002 | 0x0010   # NOSIZE|NOMOVE|NOACTIVATE
                ctypes.windll.user32.SetWindowPos(self._hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP)
            except Exception:
                pass
        elif self.mode == "desktop" and not col.IS_MAC:
            try:
                self.root.lower()
            except Exception:
                pass

    @property
    def native_desktop(self) -> bool:
        return bool(self._nswin is not None or self._hwnd)


# ---- the widget --------------------------------------------------------------
class DeskMonitor:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.W = int(self.cfg["width"])
        self.data: dict | None = None
        self.sentinel = SentinelLink(self.cfg["sentinel_dir"], int(self.cfg["sentinel_scan_every_s"]))
        self._stop = threading.Event()
        self._click_regions: list[tuple[int, int, int, int, str]] = []

        self.root = tk.Tk()
        self.root.title("DeskMonitor")
        self.root.overrideredirect(True)
        self._setup_transparency()
        self.canvas = tk.Canvas(self.root, width=self.W, height=200, highlightthickness=0,
                                bd=0, bg=self._canvas_bg)
        self.canvas.pack()
        self._fonts()

        x, y = self.cfg.get("x"), self.cfg.get("y")
        if x is None or y is None:
            x = self.root.winfo_screenwidth() - self.W - 30
            y = 60
        self.root.geometry(f"+{int(x)}+{int(y)}")
        try:
            self.root.attributes("-alpha", float(self.cfg["opacity"]))
        except Exception:
            pass

        self._bind()
        self._menu()
        self.pinner = Pinner(self.root)

        threading.Thread(target=self._metrics_loop, daemon=True).start()
        threading.Thread(target=self._sentinel_loop, daemon=True).start()
        self.root.after(150, self._after_map)
        self.root.after(400, self._tick)

    # -- setup -----------------------------------------------------------------
    def _setup_transparency(self) -> None:
        """Rounded corners: transparent window background where supported."""
        self._canvas_bg = BG
        self._rounded = False
        try:
            if col.IS_MAC:
                self.root.attributes("-transparent", True)
                self.root.config(bg="systemTransparent")
                self._canvas_bg = "systemTransparent"
                self._rounded = True
            elif col.IS_WIN:
                self.root.config(bg=KEY)
                self.root.attributes("-transparentcolor", KEY)
                self._canvas_bg = KEY
                self._rounded = True
        except tk.TclError:
            self._canvas_bg = BG

    def _fonts(self) -> None:
        fams = set(tkfont.families(self.root))
        pick = lambda *names: next((n for n in names if n in fams), "TkDefaultFont")
        ui = pick("SF Pro Text", ".AppleSystemUIFont", "Segoe UI", "Helvetica Neue", "Inter", "DejaVu Sans", "Helvetica")
        mono = pick("SF Mono", "Menlo", "Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier")
        scale = 1.0 if col.IS_MAC else 0.8   # Tk on Windows/Linux renders points larger
        sz = lambda n: max(7, round(n * scale))
        self.f_title = tkfont.Font(family=ui, size=sz(14), weight="bold")
        self.f_big = tkfont.Font(family=ui, size=sz(30), weight="bold")
        self.f_label = tkfont.Font(family=ui, size=sz(11))
        self.f_bold = tkfont.Font(family=ui, size=sz(11), weight="bold")
        self.f_small = tkfont.Font(family=ui, size=sz(10))
        self.f_sect = tkfont.Font(family=ui, size=sz(9), weight="bold")
        self.f_mono = tkfont.Font(family=mono, size=sz(10))

    def _after_map(self) -> None:
        self.pinner.setup()
        self.pinner.apply(self.cfg.get("pin_mode", "desktop"))
        self.pin_var.set(self.pinner.mode)

    def _bind(self) -> None:
        c = self.canvas
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        for ev in ("<Button-3>", "<Control-Button-1>") + (("<Button-2>",) if col.IS_MAC else ()):
            c.bind(ev, self._popup)

    def _menu(self) -> None:
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Refresh now", command=self._refresh_now)
        m.add_separator()
        m.add_command(label="Open Sentinel", command=self._open_sentinel)
        m.add_command(label="Run Sentinel self-scan now", command=self.sentinel.scan_now)
        m.add_separator()
        self.pin_var = tk.StringVar(value=self.cfg.get("pin_mode", "desktop"))
        for label, val in (("Pin to desktop (behind windows)", "desktop"),
                           ("Always on top", "top"), ("Normal window", "normal")):
            m.add_radiobutton(label=label, value=val, variable=self.pin_var,
                              command=lambda v=val: self._set_pin(v))
        op = tk.Menu(m, tearoff=0)
        for pct in (100, 94, 85, 75, 60):
            op.add_command(label=f"{pct}%", command=lambda p=pct: self._set_opacity(p / 100))
        m.add_cascade(label="Opacity", menu=op)
        m.add_command(label="Reset position", command=self._reset_pos)
        m.add_separator()
        m.add_command(label="Quit DeskMonitor", command=self.quit)
        self.menu = m

    # -- interaction -----------------------------------------------------------
    def _press(self, e):
        self._drag_from = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())
        self._moved = False

    def _drag(self, e):
        sx, sy, wx, wy = self._drag_from
        dx, dy = e.x_root - sx, e.y_root - sy
        if abs(dx) + abs(dy) > 3:
            self._moved = True
        self.root.geometry(f"+{wx + dx}+{wy + dy}")

    def _release(self, e):
        if self._moved:
            save_state(x=self.root.winfo_x(), y=self.root.winfo_y())
            return
        for x0, y0, x1, y1, action in self._click_regions:
            if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                if action == "sentinel":
                    self._open_sentinel()
                return

    def _popup(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _set_pin(self, mode: str) -> None:
        self.pinner.apply(mode)
        save_state(pin_mode=mode)

    def _set_opacity(self, a: float) -> None:
        try:
            self.root.attributes("-alpha", a)
            save_state(opacity=a)
        except Exception:
            pass

    def _reset_pos(self) -> None:
        x, y = self.root.winfo_screenwidth() - self.W - 30, 60
        self.root.geometry(f"+{x}+{y}")
        save_state(x=x, y=y)

    def _refresh_now(self) -> None:
        threading.Thread(target=lambda: self._collect() or self.sentinel.poll(), daemon=True).start()

    def _open_sentinel(self) -> None:
        threading.Thread(target=self.sentinel.open_app, daemon=True).start()

    def quit(self) -> None:
        self._stop.set()
        self.root.destroy()

    # -- background work ---------------------------------------------------------
    def _collect(self) -> None:
        try:
            self.data = col.snapshot()
            self.data["temp"] = col.temperature()
        except Exception as exc:  # never let the thread die
            print("collect error:", exc, file=sys.stderr)

    def _metrics_loop(self) -> None:
        col.cpu()                    # prime CPU counters
        col.applications()
        while not self._stop.is_set():
            self._collect()
            self._stop.wait(float(self.cfg["refresh_s"]))

    def _sentinel_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sentinel.poll()
            except Exception as exc:
                print("sentinel error:", exc, file=sys.stderr)
            self._stop.wait(10)

    # -- drawing -------------------------------------------------------------------
    def _tick(self) -> None:
        try:
            self.draw()
            self.pinner.reassert()
        except Exception as exc:
            print("draw error:", exc, file=sys.stderr)
        if not self._stop.is_set():
            self.root.after(1000, self._tick)

    def _rrect(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
               x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _text(self, x, y, s, font, fill=TEXT, anchor="nw"):
        return self.canvas.create_text(x, y, text=s, font=font, fill=fill, anchor=anchor)

    def _fit(self, s: str, font, width: int) -> str:
        if font.measure(s) <= width:
            return s
        while s and font.measure(s + "…") > width:
            s = s[:-1]
        return s + "…"

    def _bar(self, x, y, w, pct, color, h=6):
        pct = max(0.0, min(100.0, pct or 0))
        self._rrect(x, y, x + w, y + h, h / 2, fill=TRACK, outline="")
        if pct > 0:
            self._rrect(x, y, x + max(h, w * pct / 100), y + h, h / 2, fill=color, outline="")

    def _section(self, y, title, right="") -> int:
        P = 16
        self._text(P, y, title.upper(), self.f_sect, MUTED)
        if right:
            self._text(self.W - P, y, right, self.f_small, MUTED, "ne")
        return y + 18

    def _metric(self, y, label, value, pct, color, sub="") -> int:
        P = 16
        self._text(P, y, label, self.f_label)
        self._text(self.W - P, y, value, self.f_bold, color, "ne")
        y += 19
        if pct is not None:
            self._bar(P, y, self.W - 2 * P, pct, color)
            y += 10
        if sub:
            self._text(P, y, sub, self.f_small, MUTED)
            y += 16
        return y + 6

    def draw(self) -> None:
        c, W, P = self.canvas, self.W, 16
        c.delete("all")
        self._click_regions = []
        d = self.data
        bg_id = self._rrect(1, 1, W - 1, 100, 16, fill=BG, outline=LINE) if self._rounded \
            else c.create_rectangle(0, 0, W, 100, fill=BG, outline=LINE)

        # header
        y = 14
        host = d["host"] if d else platform.node().split(".")[0]
        c.create_oval(P, y + 6, P + 8, y + 14, fill=ACCENT, outline="")
        self._text(P + 14, y, self._fit(host, self.f_title, W - 110), self.f_title)
        self._text(W - P, y + 2, time.strftime("%H:%M"), self.f_title, MUTED, "ne")
        y += 24
        if not d:
            self._text(P, y, "Collecting…", self.f_small, MUTED)
            self._finish(bg_id, y + 30)
            return
        self._text(P, y, f"{d['os']}  ·  up {fmt_uptime(d['uptime'])}", self.f_small, MUTED)
        y += 26

        # system
        y = self._section(y, "System")
        cpu = d["cpu"]
        load = f"  ·  load {cpu['load1']:.2f}" if cpu.get("load1") is not None else ""
        y = self._metric(y, "CPU", f"{cpu['percent']:.0f}%", cpu["percent"],
                         level_color(cpu["percent"]), f"{cpu['cores']} cores{load}")

        t = d.get("temp") or {}
        if t.get("cpu_c") is not None:
            tc = t["cpu_c"]
            y = self._metric(y, "CPU temperature", f"{tc:.0f} °C", min(100, tc),
                             level_color(tc, 75, 90))
        else:
            state = t.get("state") or "n/a"
            colr = {"Nominal": OK, "Fair": WARN}.get(state, BAD if state not in ("n/a", "") else MUTED)
            y = self._metric(y, "Thermal state" if state != "n/a" else "Temperature",
                             state, None, colr, t.get("hint", ""))

        m = d["memory"]
        y = self._metric(y, "Memory", f"{fmt_bytes(m['used'])} / {fmt_bytes(m['total'], 0)}",
                         m["percent"], level_color(m["percent"], 75, 90),
                         f"{m['percent']:.0f}% used" + (f"  ·  swap {fmt_bytes(m['swap_used'])}" if m["swap_used"] else ""))

        for disk in d["disks"]:
            y = self._metric(y, self._fit(disk["label"], self.f_label, 130),
                             f"{fmt_bytes(disk['free'], 0)} free", disk["percent"],
                             level_color(disk["percent"], 80, 92),
                             f"{disk['percent']:.0f}% of {fmt_bytes(disk['total'], 0)} used")

        b = d.get("battery")
        if b:
            state = "charging" if b["plugged"] and b["percent"] < 100 else "on AC" if b["plugged"] else (
                f"{int(b['secs_left'] // 3600)}h {int(b['secs_left'] % 3600 // 60)}m left" if b.get("secs_left") else "on battery")
            bc = BAD if b["percent"] < 15 and not b["plugged"] else WARN if b["percent"] < 30 and not b["plugged"] else OK
            y = self._metric(y, "Battery", f"{b['percent']:.0f}%", b["percent"], bc, state)

        n = d["network"]
        self._text(P, y, "Network", self.f_label)
        self._text(W - P, y, f"↓ {fmt_rate(n['down_bps'])}   ↑ {fmt_rate(n['up_bps'])}", self.f_mono, TEXT, "ne")
        y += 28

        # apps
        c.create_line(P, y, W - P, y, fill=LINE)
        y += 10
        apps = d["apps"]
        y = self._section(y, "Top apps", f"{apps['app_count']} apps · {apps['process_count']} proc")
        colx_cpu, colx_mem = W - P - 62, W - P
        for a in apps["top"][: int(self.cfg["top_apps"])]:
            name = a["name"] + (f" ({a['count']})" if a["count"] > 1 else "")
            self._text(P, y, self._fit(name, self.f_label, W - 2 * P - 120), self.f_label)
            self._text(colx_cpu, y, f"{a['cpu']:.1f}%", self.f_mono, level_color(a["cpu"], 25, 60), "ne")
            self._text(colx_mem, y, fmt_bytes(a["mem"], 1 if a["mem"] > 1 << 30 else 0), self.f_mono, MUTED, "ne")
            y += 18
        y += 8

        # sentinel
        c.create_line(P, y, W - P, y, fill=LINE)
        y += 10
        y = self._draw_sentinel(y)

        self._text(P, y + 2, f"updated {time.strftime('%H:%M:%S', time.localtime(d['ts']))}  ·  right-click for options",
                   self.f_small, "#5c6773")
        self._finish(bg_id, y + 24)

    def _draw_sentinel(self, y0: int) -> int:
        c, W, P = self.canvas, self.W, 16
        st = self.sentinel.snapshot()
        state = st.get("state", "checking")
        pill = {"live": ("LIVE", ACCENT), "selfscan": ("SELF-SCAN", WARN), "scanning": ("SCANNING…", "#58a6ff"),
                "missing": ("NOT FOUND", BAD), "error": ("ERROR", BAD), "checking": ("CHECKING…", MUTED)}[state]
        y = y0
        self._text(P, y, "SENTINEL", self.f_sect, ACCENT)
        tw = self.f_small.measure(pill[0]) + 14
        self._rrect(W - P - tw, y - 2, W - P, y + 15, 8, fill="", outline=pill[1])
        self._text(W - P - tw / 2, y + 6, pill[0], self.f_small, pill[1], "center")
        y += 24

        if state in ("missing", "error"):
            for line in (st.get("detail", ""), "Set sentinel_dir in config.json"):
                self._text(P, y, self._fit(line, self.f_small, W - 2 * P), self.f_small, MUTED)
                y += 16
            return y + 6

        if st.get("score") is None:
            self._text(P, y, "Running first scan…" if state == "scanning" else "Waiting for first scan…",
                       self.f_small, MUTED)
            y += 22
        else:
            # score block
            score = int(st["score"])
            sc_col = OK if score >= 75 else WARN if score >= 50 else BAD
            self._text(P, y - 2, str(score), self.f_big, sc_col)
            sw = self.f_big.measure(str(score))
            self._text(P + sw + 6, y + 4, f"grade {st.get('grade', '?')}", self.f_bold, sc_col)
            self._text(P + sw + 6, y + 22, "security score", self.f_small, MUTED)
            # right column stats
            rx = W - P
            rows = [
                ("exposed ports", st.get("ports_exposed", 0), WARN if st.get("ports_close") else TEXT),
                ("flagged conns", st.get("conn_flagged", 0),
                 BAD if st.get("conn_malicious") else WARN if st.get("conn_flagged") else TEXT),
                ("failed checks", st.get("vuln_fail", 0), WARN if st.get("vuln_fail") else TEXT),
            ]
            for i, (lab, n, colr) in enumerate(rows):
                yy = y - 1 + i * 15
                self._text(rx, yy, str(n), self.f_mono, colr, "ne")
                self._text(rx - 30, yy, lab, self.f_small, MUTED, "ne")
            y += 50

            x = P
            bys = st.get("by_severity") or {}
            for sev in SEVERITIES:
                n = int(bys.get(sev, 0))
                colr = SEV_COLORS[sev] if n else "#3a4652"
                label = f"{ {'critical': 'Crit', 'high': 'High', 'medium': 'Med', 'low': 'Low'}[sev]} {n}"
                w = self.f_small.measure(label) + 14
                self._rrect(x, y, x + w, y + 18, 7, fill=TRACK, outline=colr)
                self._text(x + w / 2, y + 9, label, self.f_small, colr if n else MUTED, "center")
                x += w + 6
            y += 26

            alerts = st.get("alerts") or []
            if not alerts:
                self._text(P, y, "✓ Nothing needs attention", self.f_label, OK)
                y += 18
            for a in alerts[:2]:
                colr = SEV_COLORS.get(a["severity"], MUTED)
                c.create_rectangle(P, y + 3, P + 3, y + 13, fill=colr, outline="")
                self._text(P + 9, y, self._fit(a["title"], self.f_small, W - 2 * P - 10), self.f_small)
                y += 17

        bits = []
        if state == "live":
            bits.append("live")
        elif st.get("app_running"):
            bits.append("Sentinel open, no live link")
        else:
            bits.append("Sentinel not running")
        if st.get("learning"):
            bits.append("learning")
        if st.get("events_24h"):
            bits.append(f"{st['events_24h']} high alerts 24h")
        if st.get("updated"):
            bits.append(ago(st.get("updated")))
        self._text(P, y + 2, self._fit("  ·  ".join(bits), self.f_small, W - 2 * P), self.f_small, MUTED)
        y += 17
        hint = "open Sentinel" if st.get("app_running") else "launch Sentinel"
        self._text(P, y + 2, "› click to " + hint, self.f_small, ACCENT)
        y += 22
        self._click_regions.append((0, y0 - 6, W, y, "sentinel"))
        return y

    def _finish(self, bg_id, height: int) -> None:
        H = int(height)
        if self._rounded:
            self.canvas.delete(bg_id)
            bg_id = self._rrect(1, 1, self.W - 1, H - 1, 16, fill=BG, outline=LINE)
        else:
            self.canvas.coords(bg_id, 0, 0, self.W - 1, H - 1)
        self.canvas.tag_lower(bg_id)
        if int(self.canvas.cget("height")) != H:
            self.canvas.config(height=H)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if "--once" in sys.argv:
        col.cpu()
        col.applications()
        time.sleep(1)
        snap = col.snapshot()
        snap["temp"] = col.temperature()
        link = SentinelLink(load_config()["sentinel_dir"])
        snap["sentinel"] = link.poll()
        print(json.dumps(snap, indent=2, default=str))
        return
    DeskMonitor().run()


if __name__ == "__main__":
    main()
