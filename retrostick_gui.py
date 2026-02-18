"""
RetroStick Fix - Setup GUI
============================
A Windows GUI application for configuring arcade cabinet controller
assignments. Walks users through identifying each player's controller
by pressing a button, then saves persistent hardware identifiers.

Features:
- Auto-detect RetroBat installation
- Step-by-step player controller assignment (press a button to assign)
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
    detect_controllers_wmi, find_retrobat_path,
    write_retrostick_config, read_retrostick_config,
    apply_to_retrobat, run_startup_fix
)


# ─── Constants ──────────────────────────────────────────────────────

APP_NAME = "RetroStick Fix"
APP_VERSION = "1.0.0"
CONFIG_DIR = Path(os.path.expanduser("~")) / ".retrostick-fix"
CONFIG_FILE = CONFIG_DIR / "retrostick_config.json"

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


# ─── Input Listener (polls for button presses) ──────────────────────

class ControllerListener:
    """Listens for controller input to identify which device the user pressed.
    
    Uses Windows XInput API directly via ctypes to detect button presses
    on connected controllers. This works even when no game is running.
    """
    
    def __init__(self):
        self._running = False
        self._callback = None
        self._thread = None
        
        # Try to load XInput
        self._xinput = None
        try:
            self._xinput = ctypes.windll.xinput1_4
        except OSError:
            try:
                self._xinput = ctypes.windll.xinput1_3
            except OSError:
                try:
                    self._xinput = ctypes.windll.xinput9_1_0
                except OSError:
                    pass
    
    def start_listening(self, callback: Callable[[int, ControllerInfo], None]):
        """Start listening for any controller button press.
        
        callback receives (xinput_index, controller_info) when a button is pressed.
        """
        self._callback = callback
        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
    
    def stop_listening(self):
        """Stop listening for input."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
    
    def _listen_loop(self):
        """Poll all XInput slots for button presses."""
        if not self._xinput:
            # Fallback: just detect connected controllers
            self._listen_fallback()
            return
        
        # XINPUT_STATE structure
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
        
        # Store previous states to detect new presses
        prev_states = {}
        STICK_THRESHOLD = 20000
        
        while self._running:
            for i in range(4):  # XInput supports 0-3
                state = XINPUT_STATE()
                result = self._xinput.XInputGetState(i, ctypes.byref(state))
                
                if result == 0:  # ERROR_SUCCESS
                    buttons = state.Gamepad.wButtons
                    triggers = max(state.Gamepad.bLeftTrigger, 
                                  state.Gamepad.bRightTrigger)
                    sticks = max(
                        abs(state.Gamepad.sThumbLX),
                        abs(state.Gamepad.sThumbLY),
                        abs(state.Gamepad.sThumbRX),
                        abs(state.Gamepad.sThumbRY),
                    )
                    
                    has_input = (buttons != 0 or triggers > 128 or 
                                sticks > STICK_THRESHOLD)
                    prev_had_input = prev_states.get(i, False)
                    
                    if has_input and not prev_had_input:
                        # New button press detected on this controller!
                        ctrl = ControllerInfo(
                            device_name=f"XInput Controller {i}",
                            xinput_index=i,
                        )
                        if self._callback:
                            self._callback(i, ctrl)
                    
                    prev_states[i] = has_input
                else:
                    prev_states[i] = False
            
            time.sleep(0.05)  # 20Hz polling
    
    def _listen_fallback(self):
        """Fallback when XInput isn't available - prompt user to select."""
        # In fallback mode, we just enumerate and let user pick
        while self._running:
            time.sleep(0.1)


# ─── Main GUI Application ───────────────────────────────────────────

class RetroStickApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("820x700")
        self.root.minsize(750, 650)
        self.root.configure(bg=COLORS["bg_dark"])
        
        # Try to set icon and dark title bar
        self._setup_window()
        
        # State
        self.retrobat_path: Optional[Path] = None
        self.assignments: List[PlayerAssignment] = []
        self.detected_controllers: List[ControllerInfo] = []
        self.num_players = 2
        self.listener = ControllerListener()
        self.current_setup_player = 0
        self.setup_active = False
        
        # Load existing config
        self._load_config()
        
        # Build UI
        self._build_ui()
        
        # Auto-detect RetroBat
        self.root.after(500, self._auto_detect_retrobat)
    
    def _setup_window(self):
        """Configure window appearance."""
        # Center on screen
        self.root.update_idletasks()
        w, h = 820, 700
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        
        # Dark title bar on Windows 10/11
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
    
    def _build_ui(self):
        """Build the main UI layout."""
        # Configure styles
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
        
        ttk.Label(header, text="⬡ RETROSTICK FIX", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(header, text="Persistent arcade controller assignment for RetroBar / LaunchBox / EmulationStation",
                 style="Subtitle.TLabel").pack(anchor=tk.W)
        
        # ── RetroBat Path ──
        path_frame = ttk.Frame(main, style="Dark.TFrame")
        path_frame.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(path_frame, text="RetroBat Path:", style="Dark.TLabel").pack(side=tk.LEFT)
        
        self.path_var = tk.StringVar(value="Searching...")
        self.path_entry = tk.Entry(path_frame, textvariable=self.path_var,
                                   bg=COLORS["bg_card"], fg=COLORS["text_primary"],
                                   insertbackground=COLORS["accent_blue"],
                                   relief=tk.FLAT, font=("Consolas", 10))
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 5))
        
        browse_btn = tk.Button(path_frame, text="Browse", 
                              bg=COLORS["bg_card"], fg=COLORS["text_primary"],
                              relief=tk.FLAT, font=("Segoe UI", 9),
                              command=self._browse_retrobat,
                              cursor="hand2")
        browse_btn.pack(side=tk.RIGHT)
        
        # ── Player Count ──
        count_frame = ttk.Frame(main, style="Dark.TFrame")
        count_frame.pack(fill=tk.X, pady=(0, 15))
        
        ttk.Label(count_frame, text="Number of Players:", style="Dark.TLabel").pack(side=tk.LEFT)
        
        self.player_count_var = tk.IntVar(value=self.num_players)
        for i in range(1, 5):
            color = COLORS["player_colors"][i-1]
            rb = tk.Radiobutton(count_frame, text=str(i),
                               variable=self.player_count_var, value=i,
                               bg=COLORS["bg_dark"], fg=color,
                               selectcolor=COLORS["bg_card"],
                               activebackground=COLORS["bg_dark"],
                               activeforeground=color,
                               font=("Consolas", 12, "bold"),
                               command=self._on_player_count_change,
                               cursor="hand2")
            rb.pack(side=tk.LEFT, padx=(15, 0))
        
        # ── Controller Cards ──
        self.cards_frame = ttk.Frame(main, style="Dark.TFrame")
        self.cards_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        self.player_cards = []
        self._rebuild_player_cards()
        
        # ── Action Buttons ──
        actions = ttk.Frame(main, style="Dark.TFrame")
        actions.pack(fill=tk.X, pady=(5, 0))
        
        # Left side buttons
        left_btns = ttk.Frame(actions, style="Dark.TFrame")
        left_btns.pack(side=tk.LEFT)
        
        self.detect_btn = tk.Button(left_btns, text="🔍 Detect Controllers",
                                    bg=COLORS["bg_card"], fg=COLORS["text_primary"],
                                    relief=tk.FLAT, font=("Segoe UI", 10),
                                    command=self._detect_controllers,
                                    cursor="hand2", padx=12, pady=6)
        self.detect_btn.pack(side=tk.LEFT, padx=(0, 8))
        
        self.setup_btn = tk.Button(left_btns, text="🕹️ Setup Controllers",
                                   bg=COLORS["accent_blue"], fg=COLORS["bg_dark"],
                                   relief=tk.FLAT, font=("Segoe UI", 10, "bold"),
                                   command=self._start_setup_wizard,
                                   cursor="hand2", padx=12, pady=6)
        self.setup_btn.pack(side=tk.LEFT, padx=(0, 8))
        
        # Right side buttons
        right_btns = ttk.Frame(actions, style="Dark.TFrame")
        right_btns.pack(side=tk.RIGHT)
        
        self.apply_btn = tk.Button(right_btns, text="✓ Apply & Save",
                                   bg=COLORS["accent_green"], fg=COLORS["bg_dark"],
                                   relief=tk.FLAT, font=("Segoe UI", 10, "bold"),
                                   command=self._apply_and_save,
                                   cursor="hand2", padx=12, pady=6)
        self.apply_btn.pack(side=tk.RIGHT, padx=(8, 0))
        
        startup_btn = tk.Button(right_btns, text="⚡ Install Startup Task",
                               bg=COLORS["bg_card"], fg=COLORS["accent_yellow"],
                               relief=tk.FLAT, font=("Segoe UI", 10),
                               command=self._install_startup,
                               cursor="hand2", padx=12, pady=6)
        startup_btn.pack(side=tk.RIGHT, padx=(8, 0))
        
        # ── Status Bar ──
        self.status_var = tk.StringVar(value="Ready")
        status = ttk.Label(main, textvariable=self.status_var, style="Status.TLabel")
        status.pack(fill=tk.X, pady=(10, 0))
    
    def _rebuild_player_cards(self):
        """Rebuild the player assignment cards."""
        # Clear existing
        for widget in self.cards_frame.winfo_children():
            widget.destroy()
        self.player_cards = []
        
        n = self.player_count_var.get()
        
        for i in range(n):
            player_num = i + 1
            color = COLORS["player_colors"][i]
            
            # Card frame
            card = tk.Frame(self.cards_frame, bg=COLORS["bg_card"],
                          highlightbackground=COLORS["border"],
                          highlightthickness=1, padx=15, pady=12)
            card.pack(fill=tk.X, pady=(0, 8))
            
            # Player header with colored indicator
            header = tk.Frame(card, bg=COLORS["bg_card"])
            header.pack(fill=tk.X)
            
            indicator = tk.Label(header, text="●", fg=color, bg=COLORS["bg_card"],
                               font=("Segoe UI", 14))
            indicator.pack(side=tk.LEFT)
            
            title = tk.Label(header, text=PLAYER_LABELS[i], 
                           fg=color, bg=COLORS["bg_card"],
                           font=("Consolas", 13, "bold"))
            title.pack(side=tk.LEFT, padx=(5, 0))
            
            # Status text
            status = tk.Label(header, text="Not assigned", 
                            fg=COLORS["text_dim"], bg=COLORS["bg_card"],
                            font=("Segoe UI", 10))
            status.pack(side=tk.RIGHT)
            
            # Controller info area
            info = tk.Frame(card, bg=COLORS["bg_card"])
            info.pack(fill=tk.X, pady=(8, 0))
            
            name_label = tk.Label(info, text="Controller: —",
                                fg=COLORS["text_secondary"], bg=COLORS["bg_card"],
                                font=("Consolas", 9), anchor=tk.W)
            name_label.pack(fill=tk.X)
            
            id_label = tk.Label(info, text="Hardware ID: —",
                              fg=COLORS["text_dim"], bg=COLORS["bg_card"],
                              font=("Consolas", 8), anchor=tk.W)
            id_label.pack(fill=tk.X)
            
            self.player_cards.append({
                "card": card,
                "status": status,
                "name_label": name_label,
                "id_label": id_label,
                "color": color,
                "player_num": player_num,
            })
        
        # Update cards with any existing assignments
        self._refresh_cards()
    
    def _refresh_cards(self):
        """Update card displays with current assignment data."""
        for card_info in self.player_cards:
            p = card_info["player_num"]
            assignment = next((a for a in self.assignments if a.player_number == p), None)
            
            if assignment and assignment.controller.device_name:
                ctrl = assignment.controller
                card_info["status"].config(text="✓ Assigned", fg=COLORS["success"])
                card_info["name_label"].config(
                    text=f"Controller: {ctrl.display_name}",
                    fg=COLORS["text_primary"])
                
                hw_id = ctrl.hardware_id
                if len(hw_id) > 70:
                    hw_id = hw_id[:67] + "..."
                card_info["id_label"].config(
                    text=f"Hardware ID: {hw_id}",
                    fg=COLORS["text_dim"])
                
                card_info["card"].config(highlightbackground=card_info["color"])
            else:
                card_info["status"].config(text="Not assigned", fg=COLORS["text_dim"])
                card_info["name_label"].config(text="Controller: —", fg=COLORS["text_secondary"])
                card_info["id_label"].config(text="Hardware ID: —", fg=COLORS["text_dim"])
                card_info["card"].config(highlightbackground=COLORS["border"])
    
    def _on_player_count_change(self):
        """Handle player count radio button change."""
        self.num_players = self.player_count_var.get()
        self._rebuild_player_cards()
    
    def _auto_detect_retrobat(self):
        """Try to use saved path first, then auto-detect RetroBat installation."""
        # Use the path already loaded from config if it's valid
        if self.retrobat_path and self.retrobat_path.exists():
            self.path_var.set(str(self.retrobat_path))
            self._set_status(f"Loaded RetroBat path from config: {self.retrobat_path}", "success")
            return

        path = find_retrobat_path()
        if path:
            self.retrobat_path = path
            self.path_var.set(str(path))
            self._set_status(f"Found RetroBat at {path}", "success")
        else:
            self.path_var.set("")
            self._set_status("RetroBat not auto-detected. Use Browse to set the folder.", "warning")
    
    def _browse_retrobat(self):
        """Open folder browser for RetroBat path."""
        folder = filedialog.askdirectory(title="Select RetroBat Installation Folder")
        if folder:
            path = Path(folder)
            if (path / "retrobat.exe").exists() or (path / "retrobat-new.exe").exists():
                self.retrobat_path = path
                self.path_var.set(str(path))
                self._set_status(f"RetroBat path set: {path}", "success")
            else:
                # Check if they selected a parent folder
                for child in path.iterdir():
                    if child.name.lower() == "retrobat" and child.is_dir():
                        self.retrobat_path = child
                        self.path_var.set(str(child))
                        self._set_status(f"RetroBat path set: {child}", "success")
                        return
                
                messagebox.showwarning("Not Found",
                    "retrobat.exe was not found in the selected folder.\n"
                    "Please select the root RetroBat folder.")
    
    def _detect_controllers(self):
        """Detect all connected controllers."""
        self._set_status("Detecting controllers...", "info")
        self.detect_btn.config(state=tk.DISABLED)
        
        def detect():
            controllers = detect_controllers_wmi()
            self.root.after(0, lambda: self._on_controllers_detected(controllers))
        
        threading.Thread(target=detect, daemon=True).start()
    
    def _on_controllers_detected(self, controllers: List[ControllerInfo]):
        """Handle controller detection results."""
        self.detected_controllers = controllers
        self.detect_btn.config(state=tk.NORMAL)
        
        if controllers:
            names = ", ".join(c.display_name for c in controllers)
            self._set_status(f"Found {len(controllers)} controller(s): {names}", "success")
        else:
            self._set_status("No game controllers detected. Are they plugged in?", "warning")
    
    def _start_setup_wizard(self):
        """Start the interactive controller setup wizard."""
        if self.setup_active:
            self._cancel_setup()
            return
        
        n = self.player_count_var.get()
        
        # First detect all controllers
        self._set_status("Detecting controllers before setup...", "info")
        
        def start():
            controllers = detect_controllers_wmi()
            self.detected_controllers = controllers
            self.root.after(0, lambda: self._begin_wizard(n, controllers))
        
        threading.Thread(target=start, daemon=True).start()
    
    def _begin_wizard(self, num_players: int, controllers: List[ControllerInfo]):
        """Begin the step-by-step assignment wizard."""
        if not controllers:
            messagebox.showwarning("No Controllers",
                "No game controllers were detected.\n\n"
                "Make sure your controllers/encoder boards are plugged in "
                "and recognized by Windows.")
            return
        
        self.setup_active = True
        self.current_setup_player = 0
        self.assignments = []
        self.setup_btn.config(text="✖ Cancel Setup", bg=COLORS["error"])
        
        # Show selection dialog for each player
        self._setup_next_player(num_players, controllers)
    
    def _setup_next_player(self, total_players: int, controllers: List[ControllerInfo]):
        """Show controller selection for the next player."""
        if self.current_setup_player >= total_players:
            self._finish_setup()
            return
        
        p = self.current_setup_player + 1
        color = COLORS["player_colors"][self.current_setup_player]
        
        # Highlight current card
        for i, card_info in enumerate(self.player_cards):
            if i == self.current_setup_player:
                card_info["card"].config(highlightbackground=color, highlightthickness=2)
                card_info["status"].config(text="⟶ SELECT NOW", fg=color)
            elif i > self.current_setup_player:
                card_info["card"].config(highlightbackground=COLORS["border"], highlightthickness=1)
        
        self._set_status(
            f"Select controller for {PLAYER_LABELS[self.current_setup_player]} "
            f"({p}/{total_players})", "info")
        
        # Open selection dialog
        self._show_controller_picker(p, controllers, total_players)
    
    def _show_controller_picker(self, player_num: int, 
                                 controllers: List[ControllerInfo],
                                 total_players: int):
        """Show a dialog to pick a controller for a player."""
        # Filter out already-assigned controllers
        assigned_ids = {a.controller.instance_id for a in self.assignments}
        available = [c for c in controllers if c.instance_id not in assigned_ids]
        
        if not available:
            messagebox.showwarning("No Controllers Available",
                "All detected controllers have been assigned.\n"
                "If you need more, check your USB connections.")
            self._finish_setup()
            return
        
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Select Controller for Player {player_num}")
        dialog.geometry("550x400")
        dialog.configure(bg=COLORS["bg_dark"])
        dialog.transient(self.root)
        dialog.grab_set()
        
        # Center on parent
        dialog.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 550) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 400) // 2
        dialog.geometry(f"+{x}+{y}")
        
        color = COLORS["player_colors"][player_num - 1]
        
        # Header
        hdr = tk.Frame(dialog, bg=COLORS["bg_dark"])
        hdr.pack(fill=tk.X, padx=20, pady=(15, 10))
        
        tk.Label(hdr, text=f"● {PLAYER_LABELS[player_num - 1]}",
                fg=color, bg=COLORS["bg_dark"],
                font=("Consolas", 14, "bold")).pack(anchor=tk.W)
        tk.Label(hdr, text="Select the controller for this player position:",
                fg=COLORS["text_secondary"], bg=COLORS["bg_dark"],
                font=("Segoe UI", 10)).pack(anchor=tk.W, pady=(2, 0))
        
        # Controller list
        list_frame = tk.Frame(dialog, bg=COLORS["bg_dark"])
        list_frame.pack(fill=tk.BOTH, expand=True, padx=20)
        
        selected_var = tk.IntVar(value=-1)
        
        for idx, ctrl in enumerate(available):
            btn_frame = tk.Frame(list_frame, bg=COLORS["bg_card"],
                               padx=12, pady=10, cursor="hand2")
            btn_frame.pack(fill=tk.X, pady=3)
            
            rb = tk.Radiobutton(btn_frame, 
                               variable=selected_var, value=idx,
                               bg=COLORS["bg_card"], fg=color,
                               selectcolor=COLORS["bg_panel"],
                               activebackground=COLORS["bg_card"],
                               cursor="hand2")
            rb.pack(side=tk.LEFT)
            
            info_frame = tk.Frame(btn_frame, bg=COLORS["bg_card"])
            info_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 0))
            
            tk.Label(info_frame, text=ctrl.display_name,
                    fg=COLORS["text_primary"], bg=COLORS["bg_card"],
                    font=("Consolas", 10, "bold"), anchor=tk.W).pack(fill=tk.X)
            
            hw_text = ctrl.hardware_id
            if len(hw_text) > 60:
                hw_text = hw_text[:57] + "..."
            tk.Label(info_frame, text=hw_text,
                    fg=COLORS["text_dim"], bg=COLORS["bg_card"],
                    font=("Consolas", 8), anchor=tk.W).pack(fill=tk.X)
            
            # Make entire frame clickable
            for widget in [btn_frame, info_frame] + info_frame.winfo_children():
                widget.bind("<Button-1>", lambda e, i=idx: selected_var.set(i))
        
        # Buttons
        btn_bar = tk.Frame(dialog, bg=COLORS["bg_dark"])
        btn_bar.pack(fill=tk.X, padx=20, pady=15)
        
        def on_confirm():
            idx = selected_var.get()
            if idx < 0 or idx >= len(available):
                messagebox.showinfo("Select", "Please select a controller.")
                return
            
            ctrl = available[idx]
            self.assignments.append(PlayerAssignment(
                player_number=player_num,
                controller=ctrl
            ))
            
            # Update card
            card = self.player_cards[player_num - 1]
            card["status"].config(text="✓ Assigned", fg=COLORS["success"])
            card["name_label"].config(text=f"Controller: {ctrl.display_name}",
                                     fg=COLORS["text_primary"])
            hw_id = ctrl.hardware_id
            if len(hw_id) > 70:
                hw_id = hw_id[:67] + "..."
            card["id_label"].config(text=f"Hardware ID: {hw_id}")
            card["card"].config(highlightthickness=1)
            
            dialog.destroy()
            
            self.current_setup_player += 1
            self._setup_next_player(total_players, controllers)
        
        def on_cancel():
            dialog.destroy()
            self._cancel_setup()
        
        tk.Button(btn_bar, text="Confirm", bg=color, fg=COLORS["bg_dark"],
                 font=("Segoe UI", 10, "bold"), relief=tk.FLAT,
                 command=on_confirm, cursor="hand2",
                 padx=20, pady=5).pack(side=tk.RIGHT)
        
        tk.Button(btn_bar, text="Cancel", bg=COLORS["bg_card"], 
                 fg=COLORS["text_primary"],
                 font=("Segoe UI", 10), relief=tk.FLAT,
                 command=on_cancel, cursor="hand2",
                 padx=15, pady=5).pack(side=tk.RIGHT, padx=(0, 8))
        
        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
    
    def _finish_setup(self):
        """Complete the setup wizard."""
        self.setup_active = False
        self.setup_btn.config(text="🕹️ Setup Controllers", bg=COLORS["accent_blue"])
        self._refresh_cards()
        
        n = len(self.assignments)
        self._set_status(f"Setup complete! {n} controller(s) assigned. Click 'Apply & Save' to write config.", "success")
    
    def _cancel_setup(self):
        """Cancel the setup wizard."""
        self.setup_active = False
        self.setup_btn.config(text="🕹️ Setup Controllers", bg=COLORS["accent_blue"])
        self.listener.stop_listening()
        self._set_status("Setup cancelled.", "info")
        self._refresh_cards()
    
    def _apply_and_save(self):
        """Save configuration and apply to RetroBat."""
        if not self.assignments:
            messagebox.showinfo("No Assignments",
                "No controllers have been assigned yet.\n"
                "Use 'Setup Controllers' first.")
            return
        
        retrobat_path = self.path_var.get().strip()
        if not retrobat_path:
            messagebox.showwarning("No Path",
                "Please set the RetroBat installation path first.")
            return
        
        retrobat_path = Path(retrobat_path)
        
        # Save our config
        try:
            write_retrostick_config(
                CONFIG_FILE, 
                self.assignments,
                retrobat_path=str(retrobat_path),
                target_frontends=["retrobat"]
            )
        except Exception as e:
            messagebox.showerror("Save Error", f"Failed to save config:\n{e}")
            return
        
        # Apply to RetroBat
        try:
            success = apply_to_retrobat(self.assignments, retrobat_path)
            if success:
                self._set_status("Configuration saved and applied to RetroBat!", "success")
                messagebox.showinfo("Success",
                    "Controller assignments saved!\n\n"
                    "The configuration has been applied to RetroBat.\n"
                    "It will also be re-applied automatically at startup\n"
                    "if you install the startup task.")
            else:
                self._set_status("Config saved but some RetroBat files couldn't be updated.", "warning")
                messagebox.showwarning("Partial Success",
                    "Configuration saved, but some RetroBat config files\n"
                    "couldn't be updated. Check the log for details.")
        except Exception as e:
            self._set_status(f"Error applying config: {e}", "error")
            messagebox.showerror("Error", f"Failed to apply to RetroBat:\n{e}")
    
    def _install_startup(self):
        """Install a Windows startup task/shortcut to run the fix at boot."""
        if not CONFIG_FILE.exists():
            messagebox.showinfo("Save First",
                "Please save your controller configuration first\n"
                "(click 'Apply & Save').")
            return
        
        try:
            # Create a startup batch script
            startup_dir = Path(os.path.expanduser("~")) / "AppData" / "Roaming" / \
                          "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            
            # Get the path to our script
            if getattr(sys, 'frozen', False):
                # Running as compiled exe
                exe_path = sys.executable
                startup_cmd = f'"{exe_path}" --startup'
            else:
                # Running as Python script
                python_path = sys.executable
                script_path = Path(__file__).resolve().parent / "retrostick_startup.py"
                startup_cmd = f'"{python_path}" "{script_path}"'
            
            bat_path = startup_dir / "RetroStick_Fix.bat"
            bat_content = f'''@echo off
REM RetroStick Fix - Controller Assignment
REM This runs at startup to ensure arcade controllers are in the correct order
{startup_cmd}
'''
            bat_path.write_text(bat_content)
            
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
            messagebox.showerror("Error", f"Failed to install startup task:\n{e}")
    
    def _load_config(self):
        """Load existing configuration if available."""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                
                self.assignments = [
                    PlayerAssignment.from_dict(a) 
                    for a in data.get("assignments", [])
                ]
                
                rb_path = data.get("retrobat_path", "")
                if rb_path and Path(rb_path).exists():
                    self.retrobat_path = Path(rb_path)
                
                n = max((a.player_number for a in self.assignments), default=2)
                self.num_players = n
                
            except Exception:
                pass
    
    def _set_status(self, text: str, level: str = "info"):
        """Update the status bar."""
        colors = {
            "info": COLORS["text_dim"],
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["error"],
        }
        self.status_var.set(text)
        # Can't easily change ttk label foreground dynamically,
        # so we update via configure
        for widget in self.root.winfo_children():
            self._find_and_update_status(widget, colors.get(level, COLORS["text_dim"]))
    
    def _find_and_update_status(self, widget, color):
        """Recursively find status label and update its color."""
        try:
            if hasattr(widget, 'cget') and widget.cget('textvariable'):
                var = widget.cget('textvariable')
                if str(var) == str(self.status_var):
                    widget.configure(foreground=color)
        except Exception:
            pass
        for child in widget.winfo_children():
            self._find_and_update_status(child, color)
    
    def run(self):
        """Start the application."""
        self.root.mainloop()


# ─── Entry Point ────────────────────────────────────────────────────

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description=f"{APP_NAME} - Arcade Controller Assignment")
    parser.add_argument("--startup", action="store_true",
                       help="Run startup fix (headless, no GUI)")
    parser.add_argument("--config", type=str, default=str(CONFIG_FILE),
                       help="Path to config file")
    args = parser.parse_args()
    
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Setup logging
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(CONFIG_DIR / "retrostick.log", mode='a')
        ]
    )
    
    if args.startup:
        # Headless startup mode
        success = run_startup_fix(Path(args.config))
        sys.exit(0 if success else 1)
    else:
        # GUI mode
        app = RetroStickApp()
        app.run()


if __name__ == "__main__":
    main()
