"""
RetroStick Fix - Setup GUI
============================
A Windows GUI application for configuring arcade cabinet controller
assignments.  Each player slot has two ways to assign a controller:

  * Auto Detect  – hold any button / stick for 5 seconds and the app
                   identifies the controller automatically.
  * Manual Select – pick from a filtered device list (with an option
                    to show every detected HID device).

Features:
- Auto-detect RetroBat installation
- Per-player auto-detect (hold button 5 s) or manual selection
- Scrollable, filtered device list with "Show all devices" toggle
- Hardware ID display for verification
- Save/load configurations
- Option to install as Windows startup task
- Support for 1-4 players
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import json
import os
import sys
import threading
import time
import subprocess
import ctypes
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Callable

# Import our core module
from controller_core import (
    ControllerInfo, PlayerAssignment,
    detect_controllers_wmi, find_retrobat_path, find_launchbox_path,
    write_retrostick_config, read_retrostick_config,
    apply_to_retrobat, apply_to_launchbox, run_startup_fix,
    filter_controllers_strict,
)


# ─── Constants ──────────────────────────────────────────────────────

APP_NAME = "RetroStick Fix"
APP_VERSION = "2.0.0"
CONFIG_DIR = Path(os.path.expanduser("~")) / ".retrostick-fix"
CONFIG_FILE = CONFIG_DIR / "retrostick_config.json"

HOLD_DURATION = 5.0  # seconds the user must hold a button

# Colour palette - arcade/retro themed
COLORS = {
    "bg_dark":       "#0a0a12",
    "bg_panel":      "#12121f",
    "bg_card":       "#1a1a2e",
    "bg_card_hover": "#222240",
    "accent_blue":   "#00d4ff",
    "accent_pink":   "#ff2d75",
    "accent_green":  "#00ff88",
    "accent_yellow": "#ffd700",
    "accent_orange": "#ff8c00",
    "text_primary":  "#e8e8f0",
    "text_secondary":"#8888aa",
    "text_dim":      "#555570",
    "border":        "#2a2a45",
    "success":       "#00ff88",
    "warning":       "#ffd700",
    "error":         "#ff2d75",
    "player_colors": ["#00d4ff", "#ff2d75", "#00ff88", "#ffd700"],
}

PLAYER_LABELS = ["Player 1 (Left)", "Player 2 (Right)", "Player 3", "Player 4"]


# ─── ctypes structures for input polling ────────────────────────────

class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_ulong),
        ("Gamepad", XINPUT_GAMEPAD),
    ]


class JOYINFOEX(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint),
        ("dwFlags", ctypes.c_uint),
        ("dwXpos", ctypes.c_uint),
        ("dwYpos", ctypes.c_uint),
        ("dwZpos", ctypes.c_uint),
        ("dwRpos", ctypes.c_uint),
        ("dwUpos", ctypes.c_uint),
        ("dwVpos", ctypes.c_uint),
        ("dwButtons", ctypes.c_uint),
        ("dwButtonNumber", ctypes.c_uint),
        ("dwPOV", ctypes.c_uint),
        ("dwReserved1", ctypes.c_uint),
        ("dwReserved2", ctypes.c_uint),
    ]


class JOYCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", ctypes.c_ushort),
        ("wPid", ctypes.c_ushort),
        ("szPname", ctypes.c_wchar * 32),
        ("wXmin", ctypes.c_uint),
        ("wXmax", ctypes.c_uint),
        ("wYmin", ctypes.c_uint),
        ("wYmax", ctypes.c_uint),
        ("wZmin", ctypes.c_uint),
        ("wZmax", ctypes.c_uint),
        ("wNumButtons", ctypes.c_uint),
        ("wPeriodMin", ctypes.c_uint),
        ("wPeriodMax", ctypes.c_uint),
        ("wRmin", ctypes.c_uint),
        ("wRmax", ctypes.c_uint),
        ("wUmin", ctypes.c_uint),
        ("wUmax", ctypes.c_uint),
        ("wVmin", ctypes.c_uint),
        ("wVmax", ctypes.c_uint),
        ("wCaps", ctypes.c_uint),
        ("wMaxAxes", ctypes.c_uint),
        ("wNumAxes", ctypes.c_uint),
        ("wMaxButtons", ctypes.c_uint),
        ("szRegKey", ctypes.c_wchar * 32),
        ("szOEMVxD", ctypes.c_wchar * 260),
    ]


# ─── Input Detector ─────────────────────────────────────────────────

class InputDetector:
    """Detects controller input via Windows XInput and Multimedia Joystick APIs.

    Used for auto-detect: the user holds a button / stick direction for
    a set duration and we identify which controller they are pressing.
    XInput is tried first (covers Xbox / Brooks / most arcade encoders).
    The Windows Multimedia Joystick API is used as a fallback for
    DirectInput-only controllers.
    """

    STICK_THRESHOLD = 20000
    AXIS_FRACTION = 4        # axis must move > 1/4 of range
    RELEASE_GRACE = 0.25     # seconds before a brief drop resets the timer

    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Load XInput
        self._xinput = None
        for lib_name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self._xinput = ctypes.WinDLL(lib_name)
                break
            except OSError:
                continue

        # WMM is always available on Windows
        try:
            self._winmm = ctypes.windll.winmm
        except Exception:
            self._winmm = None

    # ── public API ──────────────────────────────────────────────

    def start_hold_detection(self, duration: float,
                             on_progress: Callable,
                             on_detected: Callable):
        """Start detecting a sustained hold.

        on_progress(source_type, source_id, fraction, device_name)
            fraction goes from 0.0 → 1.0.  source_type is None when
            no input is currently detected.

        on_detected(source_type, source_id, device_name)
            Called once when the hold reaches *duration* seconds.
        """
        self.stop()
        self._running = True
        self._thread = threading.Thread(
            target=self._hold_loop,
            args=(duration, on_progress, on_detected),
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    # ── internal ────────────────────────────────────────────────

    def _hold_loop(self, duration, on_progress, on_detected):
        sustained_key = None      # (type, id)
        sustained_start = None
        release_start = None

        while self._running:
            active = self._poll_all()

            if active:
                src = active[0]
                key = (src["type"], src["id"])
                release_start = None

                if sustained_key == key:
                    elapsed = time.time() - sustained_start
                    frac = min(elapsed / duration, 1.0)
                    on_progress(src["type"], src["id"], frac, src["name"])
                    if elapsed >= duration:
                        on_detected(src["type"], src["id"], src["name"])
                        self._running = False
                        return
                else:
                    sustained_key = key
                    sustained_start = time.time()
                    on_progress(src["type"], src["id"], 0.0, src["name"])
            else:
                if sustained_key:
                    if release_start is None:
                        release_start = time.time()
                    elif time.time() - release_start > self.RELEASE_GRACE:
                        sustained_key = None
                        sustained_start = None
                        release_start = None
                        on_progress(None, None, 0.0, "")

            time.sleep(0.04)  # ~25 Hz

    def _poll_all(self) -> list:
        active = []

        # XInput (slots 0-3) – preferred for XInput-capable devices
        if self._xinput:
            for slot in range(4):
                state = XINPUT_STATE()
                if self._xinput.XInputGetState(slot, ctypes.byref(state)) == 0:
                    gp = state.Gamepad
                    if (gp.wButtons != 0
                            or gp.bLeftTrigger > 128
                            or gp.bRightTrigger > 128
                            or abs(gp.sThumbLX) > self.STICK_THRESHOLD
                            or abs(gp.sThumbLY) > self.STICK_THRESHOLD
                            or abs(gp.sThumbRX) > self.STICK_THRESHOLD
                            or abs(gp.sThumbRY) > self.STICK_THRESHOLD):
                        active.append({
                            "type": "xinput", "id": slot,
                            "name": f"XInput Controller {slot}",
                        })

        # If XInput found something, prefer it (avoids double-counting
        # XInput devices that also appear as WMM joysticks).
        if active:
            return active

        # Windows Multimedia Joystick API – fallback for DirectInput
        if self._winmm:
            try:
                num = self._winmm.joyGetNumDevs()
                for jid in range(min(num, 16)):
                    info = JOYINFOEX()
                    info.dwSize = ctypes.sizeof(JOYINFOEX)
                    info.dwFlags = 0xFF  # JOY_RETURNALL
                    if self._winmm.joyGetPosEx(jid, ctypes.byref(info)) == 0:
                        caps = JOYCAPSW()
                        self._winmm.joyGetDevCapsW(
                            jid, ctypes.byref(caps), ctypes.sizeof(JOYCAPSW))
                        cx = (caps.wXmax + caps.wXmin) // 2
                        cy = (caps.wYmax + caps.wYmin) // 2
                        thr = max((caps.wXmax - caps.wXmin) // self.AXIS_FRACTION, 1)
                        if (info.dwButtons != 0
                                or abs(int(info.dwXpos) - cx) > thr
                                or abs(int(info.dwYpos) - cy) > thr):
                            name = (caps.szPname.strip()
                                    if caps.szPname else f"Joystick {jid}")
                            active.append({
                                "type": "joystick", "id": jid, "name": name,
                            })
            except Exception:
                pass

        return active


# ─── Main GUI Application ───────────────────────────────────────────

class RetroStickApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("820x750")
        self.root.minsize(750, 700)
        self.root.configure(bg=COLORS["bg_dark"])

        self._setup_window()

        # State
        self.retrobat_path: Optional[Path] = None
        self.launchbox_path: Optional[Path] = None
        self.assignments: List[PlayerAssignment] = []
        self.detected_controllers: List[ControllerInfo] = []
        self.num_players = 2
        self.detector = InputDetector()

        # Load existing config
        self._load_config()

        # Build UI
        self._build_ui()

        # Auto-detect frontend path
        self.root.after(500, self._auto_detect_frontend_path)

    # ── window chrome ───────────────────────────────────────────

    def _setup_window(self):
        self.root.update_idletasks()
        w, h = 820, 750
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        # Set window icon
        try:
            base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
            ico_path = os.path.join(base, "retrostick.ico")
            if os.path.isfile(ico_path):
                self.root.iconbitmap(ico_path)
        except Exception:
            pass

        if sys.platform == "win32":
            try:
                self.root.update()
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                    ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int))
            except Exception:
                pass

    # ── main layout ─────────────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")

        style.configure("Dark.TFrame", background=COLORS["bg_dark"])
        style.configure("Panel.TFrame", background=COLORS["bg_panel"])
        style.configure("Card.TFrame", background=COLORS["bg_card"])

        style.configure("Title.TLabel",
                        background=COLORS["bg_dark"],
                        foreground=COLORS["accent_blue"],
                        font=("Consolas", 20, "bold"))
        style.configure("Subtitle.TLabel",
                        background=COLORS["bg_dark"],
                        foreground=COLORS["text_secondary"],
                        font=("Consolas", 10))
        style.configure("Dark.TLabel",
                        background=COLORS["bg_dark"],
                        foreground=COLORS["text_primary"],
                        font=("Segoe UI", 10))
        style.configure("Panel.TLabel",
                        background=COLORS["bg_panel"],
                        foreground=COLORS["text_primary"],
                        font=("Segoe UI", 10))
        style.configure("Card.TLabel",
                        background=COLORS["bg_card"],
                        foreground=COLORS["text_primary"],
                        font=("Segoe UI", 10))
        style.configure("CardDim.TLabel",
                        background=COLORS["bg_card"],
                        foreground=COLORS["text_dim"],
                        font=("Consolas", 8))
        style.configure("Status.TLabel",
                        background=COLORS["bg_dark"],
                        foreground=COLORS["text_dim"],
                        font=("Consolas", 9))

        style.configure("Accent.TButton",
                        background=COLORS["accent_blue"],
                        foreground=COLORS["bg_dark"],
                        font=("Segoe UI", 10, "bold"),
                        padding=(20, 8))
        style.map("Accent.TButton",
                  background=[("active", COLORS["accent_green"])])

        style.configure("Secondary.TButton",
                        background=COLORS["bg_card"],
                        foreground=COLORS["text_primary"],
                        font=("Segoe UI", 10),
                        padding=(15, 6))
        style.map("Secondary.TButton",
                  background=[("active", COLORS["bg_card_hover"])])

        # Main container
        main = ttk.Frame(self.root, style="Dark.TFrame")
        main.pack(fill=tk.BOTH, expand=True, padx=20, pady=15)

        # ── Header ──
        header = ttk.Frame(main, style="Dark.TFrame")
        header.pack(fill=tk.X, pady=(0, 15))

        header_left = ttk.Frame(header, style="Dark.TFrame")
        header_left.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(header_left, text="RETROSTICK FIX", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(header_left,
                  text="Persistent arcade controller assignment for RetroBat / LaunchBox / EmulationStation",
                  style="Subtitle.TLabel").pack(anchor=tk.W)

        tk.Button(header, text="?  Help", bg=COLORS["bg_card"],
                  fg=COLORS["accent_yellow"], relief=tk.FLAT,
                  font=("Consolas", 11, "bold"), cursor="hand2",
                  activebackground=COLORS["bg_card_hover"],
                  activeforeground=COLORS["accent_yellow"],
                  padx=14, pady=4,
                  command=self._show_help).pack(side=tk.RIGHT, anchor=tk.NE)

        # ── Target Frontend ──
        frontend_frame = ttk.Frame(main, style="Dark.TFrame")
        frontend_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(frontend_frame, text="Target Frontend:",
                  style="Dark.TLabel").pack(side=tk.LEFT)

        self.frontend_var = tk.StringVar(
            value=getattr(self, '_saved_frontend', 'retrobat'))
        tk.Radiobutton(frontend_frame, text="RetroBat",
                       variable=self.frontend_var, value="retrobat",
                       bg=COLORS["bg_dark"], fg=COLORS["accent_blue"],
                       selectcolor=COLORS["bg_card"],
                       activebackground=COLORS["bg_dark"],
                       activeforeground=COLORS["accent_blue"],
                       font=("Consolas", 11, "bold"),
                       command=self._on_frontend_change,
                       cursor="hand2").pack(side=tk.LEFT, padx=(15, 0))
        tk.Radiobutton(frontend_frame, text="LaunchBox / BigBox",
                       variable=self.frontend_var, value="launchbox",
                       bg=COLORS["bg_dark"], fg=COLORS["accent_orange"],
                       selectcolor=COLORS["bg_card"],
                       activebackground=COLORS["bg_dark"],
                       activeforeground=COLORS["accent_orange"],
                       font=("Consolas", 11, "bold"),
                       command=self._on_frontend_change,
                       cursor="hand2").pack(side=tk.LEFT, padx=(15, 0))

        # ── Frontend Path ──
        path_frame = ttk.Frame(main, style="Dark.TFrame")
        path_frame.pack(fill=tk.X, pady=(0, 10))

        self.path_label = ttk.Label(path_frame, text="RetroBat Path:",
                                    style="Dark.TLabel")
        self.path_label.pack(side=tk.LEFT)

        self.path_var = tk.StringVar(value="Searching...")
        self.path_entry = tk.Entry(path_frame, textvariable=self.path_var,
                                   bg=COLORS["bg_card"], fg=COLORS["text_primary"],
                                   insertbackground=COLORS["accent_blue"],
                                   relief=tk.FLAT, font=("Consolas", 10))
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 5))

        tk.Button(path_frame, text="Browse",
                  bg=COLORS["bg_card"], fg=COLORS["text_primary"],
                  relief=tk.FLAT, font=("Segoe UI", 9),
                  command=self._browse_path,
                  cursor="hand2").pack(side=tk.RIGHT)

        # ── Player Count ──
        count_frame = ttk.Frame(main, style="Dark.TFrame")
        count_frame.pack(fill=tk.X, pady=(0, 15))

        ttk.Label(count_frame, text="Number of Players:", style="Dark.TLabel").pack(side=tk.LEFT)

        self.player_count_var = tk.IntVar(value=self.num_players)
        for i in range(1, 5):
            color = COLORS["player_colors"][i - 1]
            tk.Radiobutton(count_frame, text=str(i),
                           variable=self.player_count_var, value=i,
                           bg=COLORS["bg_dark"], fg=color,
                           selectcolor=COLORS["bg_card"],
                           activebackground=COLORS["bg_dark"],
                           activeforeground=color,
                           font=("Consolas", 12, "bold"),
                           command=self._on_player_count_change,
                           cursor="hand2").pack(side=tk.LEFT, padx=(15, 0))

        # ── Player Cards ──
        self.cards_frame = ttk.Frame(main, style="Dark.TFrame")
        self.cards_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.player_cards: list = []
        self._rebuild_player_cards()

        # ── Bottom Action Buttons ──
        actions = ttk.Frame(main, style="Dark.TFrame")
        actions.pack(fill=tk.X, pady=(5, 0))

        self.apply_btn = tk.Button(actions, text="Apply & Save",
                                   bg=COLORS["accent_green"], fg=COLORS["bg_dark"],
                                   relief=tk.FLAT, font=("Segoe UI", 10, "bold"),
                                   command=self._apply_and_save,
                                   cursor="hand2", padx=12, pady=6)
        self.apply_btn.pack(side=tk.RIGHT, padx=(8, 0))

        tk.Button(actions, text="Install Startup Task",
                  bg=COLORS["bg_card"], fg=COLORS["accent_yellow"],
                  relief=tk.FLAT, font=("Segoe UI", 10),
                  command=self._install_startup,
                  cursor="hand2", padx=12, pady=6).pack(side=tk.RIGHT, padx=(8, 0))

        # ── Status Bar ──
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(main, textvariable=self.status_var,
                  style="Status.TLabel").pack(fill=tk.X, pady=(10, 0))

    # ── player cards ────────────────────────────────────────────

    def _rebuild_player_cards(self):
        for w in self.cards_frame.winfo_children():
            w.destroy()
        self.player_cards = []

        n = self.player_count_var.get()
        for i in range(n):
            pnum = i + 1
            color = COLORS["player_colors"][i]

            card = tk.Frame(self.cards_frame, bg=COLORS["bg_card"],
                            highlightbackground=COLORS["border"],
                            highlightthickness=1, padx=15, pady=10)
            card.pack(fill=tk.X, pady=(0, 6))

            # header row
            hdr = tk.Frame(card, bg=COLORS["bg_card"])
            hdr.pack(fill=tk.X)

            tk.Label(hdr, text="●", fg=color, bg=COLORS["bg_card"],
                     font=("Segoe UI", 14)).pack(side=tk.LEFT)
            tk.Label(hdr, text=PLAYER_LABELS[i], fg=color,
                     bg=COLORS["bg_card"],
                     font=("Consolas", 13, "bold")).pack(side=tk.LEFT, padx=(5, 0))

            status_lbl = tk.Label(hdr, text="Not assigned",
                                  fg=COLORS["text_dim"], bg=COLORS["bg_card"],
                                  font=("Segoe UI", 10))
            status_lbl.pack(side=tk.RIGHT)

            # info row
            info = tk.Frame(card, bg=COLORS["bg_card"])
            info.pack(fill=tk.X, pady=(6, 0))

            name_lbl = tk.Label(info, text="Controller: --",
                                fg=COLORS["text_secondary"], bg=COLORS["bg_card"],
                                font=("Consolas", 9), anchor=tk.W)
            name_lbl.pack(fill=tk.X)

            id_lbl = tk.Label(info, text="Hardware ID: --",
                              fg=COLORS["text_dim"], bg=COLORS["bg_card"],
                              font=("Consolas", 8), anchor=tk.W)
            id_lbl.pack(fill=tk.X)

            # button row
            btns = tk.Frame(card, bg=COLORS["bg_card"])
            btns.pack(fill=tk.X, pady=(8, 0))

            tk.Button(btns, text="Auto Detect",
                      bg=color, fg=COLORS["bg_dark"],
                      relief=tk.FLAT, font=("Segoe UI", 9, "bold"),
                      command=lambda p=pnum: self._auto_detect_for_player(p),
                      cursor="hand2", padx=10, pady=3).pack(side=tk.LEFT, padx=(0, 6))

            tk.Button(btns, text="Manual Select",
                      bg=COLORS["bg_card_hover"], fg=COLORS["text_primary"],
                      relief=tk.FLAT, font=("Segoe UI", 9),
                      command=lambda p=pnum: self._manual_select_for_player(p),
                      cursor="hand2", padx=10, pady=3).pack(side=tk.LEFT, padx=(0, 6))

            tk.Button(btns, text="Clear",
                      bg=COLORS["bg_card_hover"], fg=COLORS["error"],
                      relief=tk.FLAT, font=("Segoe UI", 9),
                      command=lambda p=pnum: self._clear_player(p),
                      cursor="hand2", padx=10, pady=3).pack(side=tk.LEFT)

            self.player_cards.append({
                "card": card, "status": status_lbl,
                "name_label": name_lbl, "id_label": id_lbl,
                "color": color, "player_num": pnum,
            })

        self._refresh_cards()

    def _refresh_cards(self):
        for ci in self.player_cards:
            p = ci["player_num"]
            assignment = next((a for a in self.assignments if a.player_number == p), None)

            if assignment and assignment.controller.device_name:
                ctrl = assignment.controller
                ci["status"].config(text="Assigned", fg=COLORS["success"])
                ci["name_label"].config(
                    text=f"Controller: {ctrl.display_name}",
                    fg=COLORS["text_primary"])
                hw = ctrl.hardware_id
                if len(hw) > 70:
                    hw = hw[:67] + "..."
                ci["id_label"].config(text=f"Hardware ID: {hw}",
                                      fg=COLORS["text_dim"])
                ci["card"].config(highlightbackground=ci["color"])
            else:
                ci["status"].config(text="Not assigned", fg=COLORS["text_dim"])
                ci["name_label"].config(text="Controller: --",
                                        fg=COLORS["text_secondary"])
                ci["id_label"].config(text="Hardware ID: --",
                                      fg=COLORS["text_dim"])
                ci["card"].config(highlightbackground=COLORS["border"])

    def _on_player_count_change(self):
        self.num_players = self.player_count_var.get()
        self._rebuild_player_cards()

    def _on_frontend_change(self):
        frontend = self.frontend_var.get()
        if frontend == "retrobat":
            self.path_label.config(text="RetroBat Path:")
            if self.retrobat_path:
                self.path_var.set(str(self.retrobat_path))
            else:
                self.path_var.set("")
                self.root.after(100, self._auto_detect_retrobat)
        else:
            self.path_label.config(text="LaunchBox Path:")
            if self.launchbox_path:
                self.path_var.set(str(self.launchbox_path))
            else:
                self.path_var.set("")
                self.root.after(100, self._auto_detect_launchbox)

    def _auto_detect_frontend_path(self):
        frontend = self.frontend_var.get()
        if frontend == "retrobat":
            self._auto_detect_retrobat()
        else:
            self._auto_detect_launchbox()

    # ── auto-detect (hold button for N seconds) ─────────────────

    def _auto_detect_for_player(self, player_num: int):
        """Open the auto-detect dialog for a single player slot."""
        color = COLORS["player_colors"][player_num - 1]

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Auto Detect - {PLAYER_LABELS[player_num - 1]}")
        dlg.geometry("480x280")
        dlg.configure(bg=COLORS["bg_dark"])
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        # Centre on parent
        dlg.update_idletasks()
        px = self.root.winfo_x() + (self.root.winfo_width() - 480) // 2
        py = self.root.winfo_y() + (self.root.winfo_height() - 280) // 2
        dlg.geometry(f"+{px}+{py}")

        # Header
        tk.Label(dlg, text=f"● {PLAYER_LABELS[player_num - 1]}",
                 fg=color, bg=COLORS["bg_dark"],
                 font=("Consolas", 14, "bold")).pack(pady=(18, 4))

        tk.Label(dlg,
                 text="Press and hold any button or stick direction\n"
                      "on the controller you want to assign.",
                 fg=COLORS["text_secondary"], bg=COLORS["bg_dark"],
                 font=("Segoe UI", 10), justify=tk.CENTER).pack(pady=(0, 12))

        # Progress bar (canvas-drawn for custom colour)
        bar_canvas = tk.Canvas(dlg, width=400, height=24,
                               bg=COLORS["bg_card"], highlightthickness=0)
        bar_canvas.pack()
        bar_fill = bar_canvas.create_rectangle(0, 0, 0, 24, fill=color, width=0)
        bar_text = bar_canvas.create_text(200, 12, text="0.0 / 5.0 s",
                                          fill=COLORS["text_primary"],
                                          font=("Consolas", 10))

        # Detected device label
        det_var = tk.StringVar(value="Waiting for input...")
        tk.Label(dlg, textvariable=det_var,
                 fg=COLORS["text_dim"], bg=COLORS["bg_dark"],
                 font=("Consolas", 10)).pack(pady=(12, 0))

        # Cancel button
        tk.Button(dlg, text="Cancel",
                  bg=COLORS["bg_card"], fg=COLORS["text_primary"],
                  relief=tk.FLAT, font=("Segoe UI", 10),
                  command=lambda: _cancel(), cursor="hand2",
                  padx=15, pady=4).pack(pady=(14, 0))

        # --- callbacks (run on bg thread, schedule UI updates) ---

        detected_result = {}

        def _on_progress(src_type, src_id, frac, name):
            def _ui():
                if not dlg.winfo_exists():
                    return
                w = int(400 * frac)
                bar_canvas.coords(bar_fill, 0, 0, w, 24)
                secs = frac * HOLD_DURATION
                bar_canvas.itemconfig(bar_text,
                                      text=f"{secs:.1f} / {HOLD_DURATION:.1f} s")
                if src_type:
                    det_var.set(f"Detected: {name}")
                else:
                    det_var.set("Waiting for input...")
            dlg.after(0, _ui)

        def _on_detected(src_type, src_id, name):
            detected_result["type"] = src_type
            detected_result["id"] = src_id
            detected_result["name"] = name
            dlg.after(0, _finish)

        def _finish():
            if not dlg.winfo_exists():
                return
            self.detector.stop()
            dlg.destroy()
            self._resolve_auto_detect(
                player_num,
                detected_result.get("type"),
                detected_result.get("id"),
                detected_result.get("name", ""),
            )

        def _cancel():
            self.detector.stop()
            dlg.destroy()

        dlg.protocol("WM_DELETE_WINDOW", _cancel)

        # Start detection
        self.detector.start_hold_detection(
            HOLD_DURATION, _on_progress, _on_detected)

    def _resolve_auto_detect(self, player_num: int,
                             src_type: str, src_id: int, src_name: str):
        """After auto-detect completes, match the input to a WMI device."""
        self._set_status("Matching detected input to hardware...", "info")

        def _work():
            controllers = detect_controllers_wmi()
            self.detected_controllers = controllers
            self.root.after(0, lambda: _match(controllers))

        def _match(controllers):
            # Find candidate controllers that are not already assigned
            assigned_ids = {a.controller.instance_id
                           for a in self.assignments
                           if a.controller.instance_id}

            if src_type == "xinput":
                # Only consider XInput-capable devices (IG_ in instance id)
                candidates = [
                    c for c in controllers
                    if "IG_" in c.instance_id.upper()
                    and c.instance_id not in assigned_ids
                ]
            else:
                # WMM joystick – try to match by name
                candidates = [
                    c for c in controllers
                    if c.device_name and src_name.lower() in c.device_name.lower()
                    and c.instance_id not in assigned_ids
                ]
                if not candidates:
                    # Broaden to all unassigned controllers
                    candidates = [c for c in controllers
                                  if c.instance_id not in assigned_ids]

            if len(candidates) == 1:
                # Unambiguous match – store XInput index if applicable
                if src_type == "xinput":
                    candidates[0].xinput_index = src_id
                self._assign_controller(player_num, candidates[0])
                self._set_status(
                    f"Auto-detected: {candidates[0].display_name} assigned to "
                    f"{PLAYER_LABELS[player_num - 1]}", "success")
            elif len(candidates) > 1:
                # Ambiguous – let the user pick from the short list
                self._show_disambiguation_picker(
                    player_num, candidates, src_name, src_type, src_id)
            else:
                # Nothing matched – fall back to full manual list
                messagebox.showwarning(
                    "No Match",
                    f"Input was detected ({src_name}) but could not be\n"
                    "matched to a specific hardware device.\n\n"
                    "Try Manual Select instead.")

        threading.Thread(target=_work, daemon=True).start()

    def _show_disambiguation_picker(self, player_num: int,
                                     candidates: List[ControllerInfo],
                                     src_name: str,
                                     src_type: str = None,
                                     src_id: int = None):
        """Show a short picker when auto-detect is ambiguous."""
        color = COLORS["player_colors"][player_num - 1]

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Select Controller - {PLAYER_LABELS[player_num - 1]}")
        dlg.geometry("580x420")
        dlg.configure(bg=COLORS["bg_dark"])
        dlg.transient(self.root)
        dlg.grab_set()

        dlg.update_idletasks()
        px = self.root.winfo_x() + (self.root.winfo_width() - 580) // 2
        py = self.root.winfo_y() + (self.root.winfo_height() - 420) // 2
        dlg.geometry(f"+{px}+{py}")

        tk.Label(dlg, text=f"● {PLAYER_LABELS[player_num - 1]}",
                 fg=color, bg=COLORS["bg_dark"],
                 font=("Consolas", 14, "bold")).pack(pady=(15, 2))
        tk.Label(dlg,
                 text=f"Input detected from \"{src_name}\".\n"
                      "Multiple matching controllers found -- please pick one:",
                 fg=COLORS["text_secondary"], bg=COLORS["bg_dark"],
                 font=("Segoe UI", 10), justify=tk.CENTER).pack(pady=(0, 10))

        sel = tk.IntVar(value=-1)
        frame = tk.Frame(dlg, bg=COLORS["bg_dark"])
        frame.pack(fill=tk.BOTH, expand=True, padx=20)

        # Count duplicate names so we can number them
        name_counts = {}
        for ctrl in candidates:
            n = ctrl.display_name
            name_counts[n] = name_counts.get(n, 0) + 1
        name_indices = {}

        for idx, ctrl in enumerate(candidates):
            row = tk.Frame(frame, bg=COLORS["bg_card"], padx=12, pady=8,
                           cursor="hand2")
            row.pack(fill=tk.X, pady=2)
            tk.Radiobutton(row, variable=sel, value=idx,
                           bg=COLORS["bg_card"], fg=color,
                           selectcolor=COLORS["bg_panel"],
                           activebackground=COLORS["bg_card"],
                           cursor="hand2").pack(side=tk.LEFT)
            inf = tk.Frame(row, bg=COLORS["bg_card"])
            inf.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 0))

            # Add device number when names are identical
            label = ctrl.display_name
            if name_counts.get(ctrl.display_name, 1) > 1:
                dev_idx = name_indices.get(ctrl.display_name, 0) + 1
                name_indices[ctrl.display_name] = dev_idx
                label = f"{ctrl.display_name} - Device #{dev_idx}"

            tk.Label(inf, text=label,
                     fg=COLORS["text_primary"], bg=COLORS["bg_card"],
                     font=("Consolas", 10, "bold"), anchor=tk.W).pack(fill=tk.X)

            # Show port/serial info for differentiation
            port_info = ctrl.serial if ctrl.serial else "N/A"
            tk.Label(inf, text=f"Port/Serial: {port_info}",
                     fg=COLORS["text_secondary"], bg=COLORS["bg_card"],
                     font=("Consolas", 8), anchor=tk.W).pack(fill=tk.X)

            hw = ctrl.hardware_id
            if len(hw) > 60:
                hw = hw[:57] + "..."
            tk.Label(inf, text=hw, fg=COLORS["text_dim"], bg=COLORS["bg_card"],
                     font=("Consolas", 8), anchor=tk.W).pack(fill=tk.X)
            for w in [row, inf] + inf.winfo_children():
                w.bind("<Button-1>", lambda e, i=idx: sel.set(i))

        bar = tk.Frame(dlg, bg=COLORS["bg_dark"])
        bar.pack(fill=tk.X, padx=20, pady=12)

        def _ok():
            i = sel.get()
            if i < 0 or i >= len(candidates):
                messagebox.showinfo("Select", "Please select a controller.")
                return
            if src_type == "xinput" and src_id is not None:
                candidates[i].xinput_index = src_id
            dlg.destroy()
            self._assign_controller(player_num, candidates[i])
            self._set_status(
                f"{candidates[i].display_name} assigned to "
                f"{PLAYER_LABELS[player_num - 1]}", "success")

        tk.Button(bar, text="Apply", bg=COLORS["accent_green"],
                  fg=COLORS["bg_dark"],
                  font=("Segoe UI", 11, "bold"), relief=tk.FLAT,
                  command=_ok, cursor="hand2",
                  padx=24, pady=6).pack(side=tk.RIGHT)
        tk.Button(bar, text="Cancel", bg=COLORS["bg_card"],
                  fg=COLORS["text_primary"], font=("Segoe UI", 10),
                  relief=tk.FLAT, command=dlg.destroy, cursor="hand2",
                  padx=15, pady=4).pack(side=tk.RIGHT, padx=(0, 8))

        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)

    # ── manual select (scrollable, filtered list) ───────────────

    def _manual_select_for_player(self, player_num: int):
        """Show the manual controller picker (with scrollbar + filter)."""
        color = COLORS["player_colors"][player_num - 1]
        self._set_status("Detecting devices...", "info")

        def _work():
            ctrls = detect_controllers_wmi()
            self.detected_controllers = ctrls
            self.root.after(0, lambda: self._open_manual_picker(player_num, ctrls))

        threading.Thread(target=_work, daemon=True).start()

    def _open_manual_picker(self, player_num: int,
                            all_controllers: List[ControllerInfo]):
        if not all_controllers:
            messagebox.showwarning("No Devices",
                "No game controllers were detected.\n\n"
                "Make sure your controllers / encoder boards are\n"
                "plugged in and recognised by Windows.")
            self._set_status("No devices found.", "warning")
            return

        color = COLORS["player_colors"][player_num - 1]
        strict = filter_controllers_strict(all_controllers)

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Manual Select - {PLAYER_LABELS[player_num - 1]}")
        dlg.geometry("580x480")
        dlg.configure(bg=COLORS["bg_dark"])
        dlg.transient(self.root)
        dlg.grab_set()

        dlg.update_idletasks()
        px = self.root.winfo_x() + (self.root.winfo_width() - 580) // 2
        py = self.root.winfo_y() + (self.root.winfo_height() - 480) // 2
        dlg.geometry(f"+{px}+{py}")

        # Header
        tk.Label(dlg, text=f"● {PLAYER_LABELS[player_num - 1]}",
                 fg=color, bg=COLORS["bg_dark"],
                 font=("Consolas", 14, "bold")).pack(pady=(15, 2))
        tk.Label(dlg, text="Select the controller for this player position:",
                 fg=COLORS["text_secondary"], bg=COLORS["bg_dark"],
                 font=("Segoe UI", 10)).pack(pady=(0, 8))

        # Scrollable list area
        list_outer = tk.Frame(dlg, bg=COLORS["bg_dark"])
        list_outer.pack(fill=tk.BOTH, expand=True, padx=20)

        canvas = tk.Canvas(list_outer, bg=COLORS["bg_dark"],
                           highlightthickness=0)
        scrollbar = tk.Scrollbar(list_outer, orient=tk.VERTICAL,
                                 command=canvas.yview,
                                 bg=COLORS["bg_card"],
                                 troughcolor=COLORS["bg_dark"])
        inner = tk.Frame(canvas, bg=COLORS["bg_dark"])

        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Mouse-wheel scrolling
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # State for list population
        selected_var = tk.IntVar(value=-1)
        show_all_var = tk.BooleanVar(value=False)
        displayed_controllers: List[ControllerInfo] = []

        def _populate(show_all: bool):
            nonlocal displayed_controllers
            for w in inner.winfo_children():
                w.destroy()

            source = all_controllers if show_all else (strict or all_controllers)
            # Filter out already-assigned controllers
            assigned_ids = {a.controller.instance_id
                           for a in self.assignments
                           if a.controller.instance_id}
            avail = [c for c in source if c.instance_id not in assigned_ids]
            displayed_controllers = avail
            selected_var.set(-1)

            if not avail:
                tk.Label(inner, text="No available controllers.",
                         fg=COLORS["text_dim"], bg=COLORS["bg_dark"],
                         font=("Segoe UI", 10)).pack(pady=20)
                return

            # Count duplicate names for numbering
            name_counts = {}
            for ctrl in avail:
                n = ctrl.display_name
                name_counts[n] = name_counts.get(n, 0) + 1
            name_indices = {}

            for idx, ctrl in enumerate(avail):
                row = tk.Frame(inner, bg=COLORS["bg_card"],
                               padx=12, pady=8, cursor="hand2")
                row.pack(fill=tk.X, pady=2)

                rb = tk.Radiobutton(row, variable=selected_var, value=idx,
                                    bg=COLORS["bg_card"], fg=color,
                                    selectcolor=COLORS["bg_panel"],
                                    activebackground=COLORS["bg_card"],
                                    cursor="hand2")
                rb.pack(side=tk.LEFT)

                inf = tk.Frame(row, bg=COLORS["bg_card"])
                inf.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 0))

                # Add device number when names are identical
                label = ctrl.display_name
                if name_counts.get(ctrl.display_name, 1) > 1:
                    dev_idx = name_indices.get(ctrl.display_name, 0) + 1
                    name_indices[ctrl.display_name] = dev_idx
                    label = f"{ctrl.display_name} - Device #{dev_idx}"

                tk.Label(inf, text=label,
                         fg=COLORS["text_primary"], bg=COLORS["bg_card"],
                         font=("Consolas", 10, "bold"),
                         anchor=tk.W).pack(fill=tk.X)

                # Show port/serial info for differentiation
                port_info = ctrl.serial if ctrl.serial else "N/A"
                tk.Label(inf, text=f"Port/Serial: {port_info}",
                         fg=COLORS["text_secondary"], bg=COLORS["bg_card"],
                         font=("Consolas", 8), anchor=tk.W).pack(fill=tk.X)

                hw = ctrl.hardware_id
                if len(hw) > 60:
                    hw = hw[:57] + "..."
                tk.Label(inf, text=hw,
                         fg=COLORS["text_dim"], bg=COLORS["bg_card"],
                         font=("Consolas", 8), anchor=tk.W).pack(fill=tk.X)

                for w in [row, inf] + inf.winfo_children():
                    w.bind("<Button-1>", lambda e, i=idx: selected_var.set(i))

            # Reset scroll position
            canvas.yview_moveto(0)

        _populate(False)

        # "Show all devices" checkbox
        chk_frame = tk.Frame(dlg, bg=COLORS["bg_dark"])
        chk_frame.pack(fill=tk.X, padx=20, pady=(6, 0))

        n_strict = len([c for c in strict
                        if c.instance_id not in
                        {a.controller.instance_id for a in self.assignments
                         if a.controller.instance_id}])
        n_all = len([c for c in all_controllers
                     if c.instance_id not in
                     {a.controller.instance_id for a in self.assignments
                      if a.controller.instance_id}])

        chk = tk.Checkbutton(
            chk_frame,
            text=f"Show all devices ({n_all} total, {n_strict} likely controllers)",
            variable=show_all_var,
            bg=COLORS["bg_dark"], fg=COLORS["text_secondary"],
            selectcolor=COLORS["bg_card"],
            activebackground=COLORS["bg_dark"],
            activeforeground=COLORS["text_secondary"],
            font=("Segoe UI", 9),
            command=lambda: _populate(show_all_var.get()),
        )
        chk.pack(anchor=tk.W)

        # Buttons
        bar = tk.Frame(dlg, bg=COLORS["bg_dark"])
        bar.pack(fill=tk.X, padx=20, pady=12)

        def _confirm():
            i = selected_var.get()
            if i < 0 or i >= len(displayed_controllers):
                messagebox.showinfo("Select", "Please select a controller.")
                return
            ctrl = displayed_controllers[i]
            # Unbind mousewheel before closing
            canvas.unbind_all("<MouseWheel>")
            dlg.destroy()
            self._assign_controller(player_num, ctrl)
            self._set_status(
                f"{ctrl.display_name} assigned to "
                f"{PLAYER_LABELS[player_num - 1]}", "success")

        def _close():
            canvas.unbind_all("<MouseWheel>")
            dlg.destroy()

        tk.Button(bar, text="Apply", bg=COLORS["accent_green"],
                  fg=COLORS["bg_dark"],
                  font=("Segoe UI", 11, "bold"), relief=tk.FLAT,
                  command=_confirm, cursor="hand2",
                  padx=24, pady=6).pack(side=tk.RIGHT)
        tk.Button(bar, text="Cancel", bg=COLORS["bg_card"],
                  fg=COLORS["text_primary"], font=("Segoe UI", 10),
                  relief=tk.FLAT, command=_close, cursor="hand2",
                  padx=15, pady=4).pack(side=tk.RIGHT, padx=(0, 8))

        dlg.protocol("WM_DELETE_WINDOW", _close)
        self._set_status(
            f"Showing {n_strict} controller(s) ({n_all} total devices detected)",
            "info")

    # ── assignment helpers ──────────────────────────────────────

    def _assign_controller(self, player_num: int, ctrl: ControllerInfo):
        """Assign a controller to a player slot (add or replace)."""
        # Remove any existing assignment for this player
        self.assignments = [a for a in self.assignments
                            if a.player_number != player_num]
        self.assignments.append(PlayerAssignment(
            player_number=player_num, controller=ctrl))
        self._refresh_cards()

    def _clear_player(self, player_num: int):
        self.assignments = [a for a in self.assignments
                            if a.player_number != player_num]
        self._refresh_cards()
        self._set_status(f"{PLAYER_LABELS[player_num - 1]} cleared.", "info")

    # ── RetroBat path ───────────────────────────────────────────

    def _auto_detect_retrobat(self):
        if self.retrobat_path and self.retrobat_path.exists():
            self.path_var.set(str(self.retrobat_path))
            self._set_status(
                f"Loaded RetroBat path from config: {self.retrobat_path}",
                "success")
            return

        path = find_retrobat_path()
        if path:
            self.retrobat_path = path
            self.path_var.set(str(path))
            self._set_status(f"Found RetroBat at {path}", "success")
        else:
            self.path_var.set("")
            self._set_status(
                "RetroBat not auto-detected. Use Browse to set the folder.",
                "warning")

    def _auto_detect_launchbox(self):
        if self.launchbox_path and self.launchbox_path.exists():
            self.path_var.set(str(self.launchbox_path))
            self._set_status(
                f"Loaded LaunchBox path from config: {self.launchbox_path}",
                "success")
            return

        path = find_launchbox_path()
        if path:
            self.launchbox_path = path
            self.path_var.set(str(path))
            self._set_status(f"Found LaunchBox at {path}", "success")
        else:
            self.path_var.set("")
            self._set_status(
                "LaunchBox not auto-detected. Use Browse to set the folder.",
                "warning")

    def _browse_path(self):
        frontend = self.frontend_var.get()
        if frontend == "retrobat":
            title = "Select RetroBat Installation Folder"
            exes = ["retrobat.exe", "retrobat-new.exe"]
            label = "RetroBat"
        else:
            title = "Select LaunchBox Installation Folder"
            exes = ["LaunchBox.exe", "BigBox.exe"]
            label = "LaunchBox"

        folder = filedialog.askdirectory(title=title)
        if not folder:
            return

        path = Path(folder)
        found = any((path / exe).exists() for exe in exes)

        # Check subdirectories if not found at root
        if not found:
            try:
                for child in path.iterdir():
                    if child.is_dir() and any(
                            (child / exe).exists() for exe in exes):
                        path = child
                        found = True
                        break
            except OSError:
                pass

        if found:
            if frontend == "retrobat":
                self.retrobat_path = path
            else:
                self.launchbox_path = path
            self.path_var.set(str(path))
            self._set_status(f"{label} path set: {path}", "success")
        else:
            messagebox.showwarning(
                "Not Found",
                f"Expected executables not found in the selected folder.\n"
                f"Please select the root {label} folder.")

    # ── save / apply ────────────────────────────────────────────

    def _apply_and_save(self):
        if not self.assignments:
            messagebox.showinfo("No Assignments",
                "No controllers have been assigned yet.\n"
                "Use Auto Detect or Manual Select for each player.")
            return

        frontend = self.frontend_var.get()
        frontend_label = "RetroBat" if frontend == "retrobat" else "LaunchBox"

        frontend_path = self.path_var.get().strip()
        if not frontend_path:
            messagebox.showwarning("No Path",
                f"Please set the {frontend_label} installation path first.")
            return

        frontend_path = Path(frontend_path)

        # Store the path on the correct attribute
        if frontend == "retrobat":
            self.retrobat_path = frontend_path
        else:
            self.launchbox_path = frontend_path

        try:
            write_retrostick_config(
                CONFIG_FILE, self.assignments,
                retrobat_path=str(frontend_path),
                target_frontends=[frontend])
        except Exception as e:
            messagebox.showerror("Save Error", f"Failed to save config:\n{e}")
            return

        try:
            if frontend == "retrobat":
                success = apply_to_retrobat(self.assignments, frontend_path)
            else:
                success = apply_to_launchbox(self.assignments, frontend_path)

            if success:
                self._set_status(
                    f"Configuration saved and applied to {frontend_label}!",
                    "success")
                messagebox.showinfo("Success",
                    "Controller assignments saved!\n\n"
                    f"The configuration has been applied to {frontend_label}.\n"
                    "It will also be re-applied automatically at startup\n"
                    "if you install the startup task.")
            else:
                self._set_status(
                    f"Config saved but some {frontend_label} files "
                    "couldn't be updated.", "warning")
                messagebox.showwarning("Partial Success",
                    f"Configuration saved, but some {frontend_label} config "
                    "files\ncouldn't be updated. Check the log for details.")
        except Exception as e:
            self._set_status(f"Error applying config: {e}", "error")
            messagebox.showerror("Error",
                                 f"Failed to apply to {frontend_label}:\n{e}")

    # ── startup task ────────────────────────────────────────────

    def _install_startup(self):
        if not CONFIG_FILE.exists():
            messagebox.showinfo("Save First",
                "Please save your controller configuration first\n"
                "(click 'Apply & Save').")
            return

        try:
            startup_dir = (Path(os.path.expanduser("~"))
                           / "AppData" / "Roaming" / "Microsoft"
                           / "Windows" / "Start Menu" / "Programs" / "Startup")

            if getattr(sys, 'frozen', False):
                exe_path = sys.executable
                startup_cmd = f'"{exe_path}" --startup'
            else:
                python_path = sys.executable
                script_path = (Path(__file__).resolve().parent
                               / "retrostick_startup.py")
                startup_cmd = f'"{python_path}" "{script_path}"'

            bat_path = startup_dir / "RetroStick_Fix.bat"
            bat_path.write_text(
                "@echo off\n"
                "REM RetroStick Fix - Controller Assignment\n"
                "REM Runs at startup to ensure arcade controllers "
                "are in the correct order\n"
                f"{startup_cmd}\n")

            self._set_status("Startup task installed!", "success")
            messagebox.showinfo("Startup Installed",
                f"Startup script installed to:\n{bat_path}\n\n"
                "The controller fix will now run automatically\n"
                "when Windows starts.")

        except PermissionError:
            messagebox.showerror("Permission Error",
                "Couldn't write to the Startup folder.\n"
                "Try running as administrator.")
        except Exception as e:
            messagebox.showerror("Error",
                                 f"Failed to install startup task:\n{e}")

    # ── config persistence ──────────────────────────────────────

    def _load_config(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)

                self.assignments = [
                    PlayerAssignment.from_dict(a)
                    for a in data.get("assignments", [])
                ]

                frontends = data.get("target_frontends", ["retrobat"])
                if "launchbox" in frontends:
                    self._saved_frontend = "launchbox"
                else:
                    self._saved_frontend = "retrobat"

                saved_path = data.get("retrobat_path", "")
                if saved_path and Path(saved_path).exists():
                    if self._saved_frontend == "launchbox":
                        self.launchbox_path = Path(saved_path)
                    else:
                        self.retrobat_path = Path(saved_path)

                n = max((a.player_number for a in self.assignments), default=2)
                self.num_players = n
            except Exception:
                pass

    # ── status bar ──────────────────────────────────────────────

    def _set_status(self, text: str, level: str = "info"):
        colors = {
            "info":    COLORS["text_dim"],
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error":   COLORS["error"],
        }
        self.status_var.set(text)
        for widget in self.root.winfo_children():
            self._find_and_update_status(
                widget, colors.get(level, COLORS["text_dim"]))

    def _find_and_update_status(self, widget, color):
        try:
            if hasattr(widget, 'cget') and widget.cget('textvariable'):
                if str(widget.cget('textvariable')) == str(self.status_var):
                    widget.configure(foreground=color)
        except Exception:
            pass
        for child in widget.winfo_children():
            self._find_and_update_status(child, color)

    # ── help window ──────────────────────────────────────────────

    def _show_help(self):
        hw = tk.Toplevel(self.root)
        hw.title("RetroStick Fix - Help")
        hw.configure(bg=COLORS["bg_dark"])
        hw.resizable(True, True)

        w, h = 680, 620
        x = self.root.winfo_x() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - h) // 2
        hw.geometry(f"{w}x{h}+{x}+{y}")

        try:
            base = getattr(sys, '_MEIPASS',
                           os.path.dirname(os.path.abspath(__file__)))
            ico = os.path.join(base, "retrostick.ico")
            if os.path.isfile(ico):
                hw.iconbitmap(ico)
        except Exception:
            pass

        # Scrollable content
        canvas = tk.Canvas(hw, bg=COLORS["bg_dark"], highlightthickness=0)
        scrollbar = tk.Scrollbar(hw, orient=tk.VERTICAL, command=canvas.yview)
        content = tk.Frame(canvas, bg=COLORS["bg_dark"], padx=25, pady=15)

        content.bind("<Configure>",
                     lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=content, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Mouse-wheel scrolling
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        hw.bind("<Destroy>",
                lambda _e: canvas.unbind_all("<MouseWheel>") if _e.widget is hw else None)

        # ── Helper to add styled sections ──
        def heading(text):
            tk.Label(content, text=text, fg=COLORS["accent_blue"],
                     bg=COLORS["bg_dark"], font=("Consolas", 14, "bold"),
                     anchor=tk.W).pack(fill=tk.X, pady=(18, 4))
            tk.Frame(content, bg=COLORS["border"], height=1).pack(fill=tk.X)

        def para(text):
            tk.Label(content, text=text, fg=COLORS["text_primary"],
                     bg=COLORS["bg_dark"], font=("Segoe UI", 10),
                     anchor=tk.W, justify=tk.LEFT,
                     wraplength=600).pack(fill=tk.X, pady=(6, 0))

        def bullet(text):
            tk.Label(content, text=f"  \u2022  {text}", fg=COLORS["text_secondary"],
                     bg=COLORS["bg_dark"], font=("Segoe UI", 10),
                     anchor=tk.W, justify=tk.LEFT,
                     wraplength=580).pack(fill=tk.X, pady=(2, 0))

        def step(number, text):
            f = tk.Frame(content, bg=COLORS["bg_dark"])
            f.pack(fill=tk.X, pady=(4, 0))
            tk.Label(f, text=f" {number} ", fg=COLORS["bg_dark"],
                     bg=COLORS["accent_blue"], font=("Consolas", 10, "bold"),
                     padx=4, pady=1).pack(side=tk.LEFT, padx=(4, 8))
            tk.Label(f, text=text, fg=COLORS["text_primary"],
                     bg=COLORS["bg_dark"], font=("Segoe UI", 10),
                     anchor=tk.W, justify=tk.LEFT,
                     wraplength=560).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ── Content ──
        tk.Label(content, text="RETROSTICK FIX", fg=COLORS["accent_blue"],
                 bg=COLORS["bg_dark"],
                 font=("Consolas", 18, "bold")).pack(anchor=tk.W)
        tk.Label(content, text=f"Version {APP_VERSION}",
                 fg=COLORS["text_dim"], bg=COLORS["bg_dark"],
                 font=("Consolas", 9)).pack(anchor=tk.W, pady=(0, 4))

        heading("What is this?")
        para("RetroStick Fix solves a common problem with arcade cabinets "
             "running on Windows: every time you reboot, Windows can "
             "randomly shuffle which USB controller is Player 1 and "
             "which is Player 2.")
        para("This tool identifies your controllers by their unique "
             "hardware IDs (not the order Windows finds them) and writes "
             "the correct player assignments to your frontend config "
             "files every time.")

        heading("Quick Start")
        step("1", "Select your frontend (RetroBat or LaunchBox/BigBox) "
             "and set the installation path. The app tries to find it "
             "automatically.")
        step("2", "Choose how many players your cabinet supports (1-4).")
        step("3", "For each player, assign a controller using one of "
             "two methods:")
        bullet("Auto Detect - press and hold any button or move the "
               "stick on the controller you want for that player. Hold "
               "for 5 seconds until it is recognised.")
        bullet("Manual Select - pick from a list of all detected "
               "controllers. Useful if auto-detect has trouble "
               "differentiating identical devices.")
        step("4", "Click \"Apply & Save\" to write the assignments to "
             "your frontend's config files.")
        step("5", "Click \"Install Startup Task\" so the fix runs "
             "automatically every time Windows boots, before your "
             "frontend launches.")

        heading("How does it work?")
        para("Each USB controller has a unique hardware identifier "
             "that includes a Vendor ID (VID), Product ID (PID), and an "
             "instance-specific serial number or port path. These stay "
             "the same across reboots.")
        para("RetroStick Fix saves which hardware ID goes with which "
             "player number. On startup, it detects the controllers, "
             "matches them by hardware ID, and writes the correct "
             "order into the frontend config files (e.g. es_input.cfg "
             "and es_settings.cfg for RetroBat, or retroarch.cfg for "
             "LaunchBox).")

        heading("Identical Controllers")
        para("If you have two identical encoder boards (same make and "
             "model), they will have the same VID and PID. The app "
             "differentiates them using their unique instance ID "
             "(serial number or USB port path). When assigning, the "
             "picker shows device numbers and port/serial info to "
             "help you tell them apart.")
        para("Tip: keep your controllers plugged into the same USB "
             "ports so their instance IDs stay consistent.")

        heading("Startup Task")
        para("The \"Install Startup Task\" button places a small .bat "
             "file in your Windows Startup folder. This runs the "
             "controller fix silently each time you log in, before "
             "your frontend launches. No admin rights are required.")

        heading("Config Files")
        para("Your settings are saved to:")
        bullet("%USERPROFILE%\\.retrostick-fix\\retrostick_config.json")
        para("Logs are written to:")
        bullet("%USERPROFILE%\\.retrostick-fix\\retrostick.log")

        heading("Troubleshooting")
        bullet("Controllers not detected? Make sure they show up in "
               "Windows Device Manager and try running as administrator.")
        bullet("Config not taking effect? Close your frontend before "
               "clicking Apply & Save.")
        bullet("Still swapping after reboot? Check that the startup "
               "task is installed (look in Task Manager > Startup tab).")
        bullet("Moved a controller to a different USB port? Re-run "
               "setup - the instance ID changes with the port.")

        # Close button
        tk.Frame(content, bg=COLORS["bg_dark"], height=15).pack()
        tk.Button(content, text="Close", bg=COLORS["bg_card"],
                  fg=COLORS["text_primary"], relief=tk.FLAT,
                  font=("Segoe UI", 10), cursor="hand2",
                  padx=20, pady=6,
                  command=hw.destroy).pack(pady=(5, 10))

        hw.transient(self.root)
        hw.grab_set()
        hw.focus_set()

    # ── run ─────────────────────────────────────────────────────

    def run(self):
        self.root.mainloop()


# ─── Entry Point ────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description=f"{APP_NAME} - Arcade Controller Assignment")
    parser.add_argument("--startup", action="store_true",
                        help="Run startup fix (headless, no GUI)")
    parser.add_argument("--config", type=str, default=str(CONFIG_FILE),
                        help="Path to config file")
    args = parser.parse_args()

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(CONFIG_DIR / "retrostick.log", mode='a'),
        ],
    )

    if args.startup:
        success = run_startup_fix(Path(args.config))
        sys.exit(0 if success else 1)
    else:
        app = RetroStickApp()
        app.run()


if __name__ == "__main__":
    main()
