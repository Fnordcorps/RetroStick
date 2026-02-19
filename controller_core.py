"""
RetroStick Fix - Controller Core Module
========================================
Core logic for detecting USB game controllers on Windows,
identifying them by persistent hardware identifiers, and
managing player assignments across reboots.

This module handles:
- Enumerating connected game controllers via Windows APIs
- Extracting persistent identifiers (VID, PID, serial, device path)
- Matching controllers to saved player assignments
- Writing assignments to RetroBat/EmulationStation config files

The key insight: Windows USB device instance paths contain the VID
(Vendor ID) and PID (Product ID) which are hardcoded in the device
firmware, plus a serial number or port-based instance ID. For arcade
encoder boards (like Brooks Universal), the VID+PID combo is the same
for all boards of that model, but the instance portion (serial or
port path) differentiates individual units.

We use a combination of:
  1. VID + PID (identifies the device model)
  2. Instance ID / Serial (differentiates identical devices)
  3. SDL GUID (what EmulationStation uses internally)
  4. Device name (human-readable fallback)
"""

import os
import sys
import json
import re
import subprocess
import ctypes
import xml.etree.ElementTree as ET
from xml.dom import minidom
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Tuple
from pathlib import Path
import logging

logger = logging.getLogger("retrostick")


# ─── Controller Filtering ───────────────────────────────────────────

# Name fragments that almost certainly indicate a non-controller HID device
_NON_CONTROLLER_KEYWORDS = [
    "mouse", "keyboard", "touchpad", "trackpad", "touch screen",
    "webcam", "camera", "microphone", "speaker", "headset", "audio",
    "fingerprint", "bluetooth radio", "wireless receiver", "usb hub",
    "disk", "storage", "mass storage", "memory", "card reader",
    "monitor", "display", "printer", "scanner",
    "network", "ethernet", "wi-fi", "sensor",
]

# Name fragments that strongly suggest a game controller
_CONTROLLER_KEYWORDS = [
    "controller", "gamepad", "joystick", "arcade", "fight stick",
    "fightstick", "xbox", "playstation", "dualshock", "dualsense",
    "brooks", "brook", "hori", "qanba", "razer", "madcatz",
    "8bitdo", "pro controller", "joycon", "joy-con", "xinput",
]


def filter_controllers_strict(controllers: List['ControllerInfo']) -> List['ControllerInfo']:
    """Return only devices that are very likely game controllers.

    Uses XInput interface markers and name-based heuristics to
    separate real controllers from mice, keyboards, and other
    HID devices that the broad WMI query picks up.
    """
    result = []
    for ctrl in controllers:
        # XInput devices are definitely game controllers
        if "IG_" in ctrl.instance_id.upper():
            result.append(ctrl)
            continue

        name = ctrl.device_name.lower()

        # Skip known non-controllers
        if any(kw in name for kw in _NON_CONTROLLER_KEYWORDS):
            continue

        # Include if name matches controller keywords
        if any(kw in name for kw in _CONTROLLER_KEYWORDS):
            result.append(ctrl)
            continue

    return result


# ─── Data Classes ───────────────────────────────────────────────────

@dataclass
class ControllerInfo:
    """Represents a detected game controller with all identifying info."""
    device_name: str = ""
    vid: str = ""           # Vendor ID (hex, e.g. "045E")
    pid: str = ""           # Product ID (hex, e.g. "02FF")
    instance_id: str = ""   # Full Windows device instance path
    serial: str = ""        # Serial number if available
    sdl_guid: str = ""      # SDL2 GUID string
    xinput_index: int = -1  # XInput slot (0-3) if applicable
    device_path: str = ""   # Raw device path from Windows
    
    @property
    def hardware_id(self) -> str:
        """A composite identifier that persists across reboots.
        
        Uses VID+PID+instance to uniquely identify a specific physical
        controller, even among multiple identical models.
        """
        if self.instance_id:
            return self.instance_id
        if self.vid and self.pid and self.serial:
            return f"USB\\VID_{self.vid}&PID_{self.pid}\\{self.serial}"
        if self.vid and self.pid:
            return f"USB\\VID_{self.vid}&PID_{self.pid}"
        return self.device_name
    
    @property
    def display_name(self) -> str:
        """Human-readable name for the controller."""
        name = self.device_name or "Unknown Controller"
        if self.vid and self.pid:
            name += f" [{self.vid}:{self.pid}]"
        return name

    def matches(self, other: 'ControllerInfo') -> bool:
        """Check if this controller matches another by hardware identity.
        
        Matching priority:
        1. Full instance ID (most specific - includes serial/port)
        2. VID + PID + Serial
        3. VID + PID only (least specific - same model, any unit)
        """
        # Full instance ID match (best)
        if self.instance_id and other.instance_id:
            # Normalize for comparison
            a = self.instance_id.upper().strip()
            b = other.instance_id.upper().strip()
            if a == b:
                return True
        
        # VID + PID + Serial match
        if (self.vid and self.pid and self.serial and
            other.vid and other.pid and other.serial):
            if (self.vid.upper() == other.vid.upper() and
                self.pid.upper() == other.pid.upper() and
                self.serial.upper() == other.serial.upper()):
                return True
        
        # VID + PID match (same model - used as fallback)
        if self.vid and self.pid and other.vid and other.pid:
            if (self.vid.upper() == other.vid.upper() and
                self.pid.upper() == other.pid.upper()):
                return True
        
        return False


@dataclass
class PlayerAssignment:
    """Maps a physical controller to a player slot."""
    player_number: int       # 1-4
    controller: ControllerInfo = field(default_factory=ControllerInfo)
    
    def to_dict(self) -> dict:
        return {
            "player_number": self.player_number,
            "controller": asdict(self.controller)
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'PlayerAssignment':
        ctrl_data = data.get("controller", {})
        ctrl = ControllerInfo(**ctrl_data)
        return cls(player_number=data["player_number"], controller=ctrl)


# ─── Config File Paths ──────────────────────────────────────────────

def find_retrobat_path() -> Optional[Path]:
    """Auto-detect RetroBat installation path.
    
    Checks common locations and the registry.
    """
    # Check common install locations
    common_paths = [
        Path("C:/RetroBat"),
        Path("D:/RetroBat"),
        Path("E:/RetroBat"),
        Path(os.path.expanduser("~/RetroBat")),
    ]
    
    for p in common_paths:
        if (p / "retrobat.exe").exists() or (p / "retrobat-new.exe").exists():
            return p
    
    # Check all drive roots
    if sys.platform == "win32":
        import string
        for letter in string.ascii_uppercase:
            p = Path(f"{letter}:/RetroBat")
            if (p / "retrobat.exe").exists() or (p / "retrobat-new.exe").exists():
                return p
    
    return None


def get_es_input_cfg_path(retrobat_path: Path) -> Path:
    """Get the path to EmulationStation's es_input.cfg."""
    return retrobat_path / "emulationstation" / ".emulationstation" / "es_input.cfg"


def get_es_settings_cfg_path(retrobat_path: Path) -> Path:
    """Get the path to EmulationStation's es_settings.cfg."""
    return retrobat_path / "emulationstation" / ".emulationstation" / "es_settings.cfg"


def find_launchbox_path() -> Optional[Path]:
    """Auto-detect LaunchBox installation path."""
    common_paths = [
        Path("C:/LaunchBox"),
        Path("D:/LaunchBox"),
        Path("E:/LaunchBox"),
        Path(os.path.expanduser("~/LaunchBox")),
    ]

    for p in common_paths:
        if (p / "LaunchBox.exe").exists() or (p / "BigBox.exe").exists():
            return p

    if sys.platform == "win32":
        import string
        for letter in string.ascii_uppercase:
            p = Path(f"{letter}:/LaunchBox")
            if (p / "LaunchBox.exe").exists() or (p / "BigBox.exe").exists():
                return p

    return None


def get_retroarch_cfg_path(launchbox_path: Path) -> Optional[Path]:
    """Find RetroArch config within or alongside a LaunchBox installation."""
    candidates = [
        launchbox_path / "ThirdParty" / "RetroArch" / "retroarch.cfg",
        launchbox_path / "Emulators" / "RetroArch" / "retroarch.cfg",
        launchbox_path / "RetroArch" / "retroarch.cfg",
    ]

    for p in candidates:
        if p.exists():
            return p

    # Check common standalone RetroArch paths
    if sys.platform == "win32":
        import string
        for letter in string.ascii_uppercase:
            for sub in ["RetroArch", "RetroArch-Win64"]:
                p = Path(f"{letter}:/{sub}/retroarch.cfg")
                if p.exists():
                    return p

    return None


# ─── Windows Controller Detection ───────────────────────────────────

def detect_controllers_wmi() -> List[ControllerInfo]:
    """Detect game controllers using Windows WMI/PowerShell.
    
    This queries Windows device management to find all connected
    game controllers with their hardware identifiers.
    """
    controllers = []
    
    if sys.platform != "win32":
        logger.warning("Controller detection requires Windows")
        return controllers
    
    try:
        # PowerShell script to enumerate game controllers
        ps_script = '''
$ErrorActionPreference = "SilentlyContinue"

# Method 1: Get XInput/HID game controllers via PnP
$gameControllers = @()

# Get devices from "Xbox Gaming Device" and HID game controllers
$hidDevices = Get-PnpDevice -Class "XnaComposite","XboxComposite","HIDClass" -Status "OK" 2>$null
$usbDevices = Get-PnpDevice -Class "USB" -Status "OK" 2>$null

# Also check via WMI for game controllers
$wmiControllers = Get-WmiObject -Class Win32_PnPEntity | Where-Object {
    $_.PNPClass -match "XInput|HID" -or
    $_.Name -match "controller|gamepad|joystick|arcade|xbox|playstation|xinput" -or
    $_.Compatible -match "HID_DEVICE_SYSTEM_GAME"
} 2>$null

# Combine and deduplicate
$allDevices = @()
if ($hidDevices) { $allDevices += $hidDevices }
if ($wmiControllers) { $allDevices += $wmiControllers }

$seen = @{}
foreach ($dev in $allDevices) {
    $instanceId = if ($dev.InstanceId) { $dev.InstanceId } else { $dev.DeviceID }
    if (-not $instanceId -or $seen.ContainsKey($instanceId)) { continue }
    
    $name = if ($dev.FriendlyName) { $dev.FriendlyName } 
            elseif ($dev.Name) { $dev.Name }
            else { "Unknown" }
    
    # Filter to likely game controllers
    $isController = $false
    if ($name -match "controller|gamepad|joystick|arcade|xbox|playstation|xinput|game|brooks|brook|encoder|stick") {
        $isController = $true
    }
    if ($instanceId -match "IG_") {
        $isController = $true  # XInput interface
    }
    
    if ($isController) {
        $seen[$instanceId] = $true
        
        # Extract VID and PID
        $vid = ""
        $pid = ""
        $serial = ""
        if ($instanceId -match "VID_([0-9A-Fa-f]{4})") { $vid = $Matches[1] }
        if ($instanceId -match "PID_([0-9A-Fa-f]{4})") { $pid = $Matches[1] }
        
        # Extract serial/instance portion (after the last backslash)
        $parts = $instanceId -split "\\\\"
        if ($parts.Count -ge 3) { $serial = $parts[-1] }
        
        $obj = [PSCustomObject]@{
            Name = $name
            InstanceId = $instanceId
            VID = $vid.ToUpper()
            PID = $pid.ToUpper()
            Serial = $serial
        }
        Write-Output ($obj | ConvertTo-Json -Compress)
    }
}
'''
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if not line or not line.startswith('{'):
                    continue
                try:
                    data = json.loads(line)
                    ctrl = ControllerInfo(
                        device_name=data.get("Name", "Unknown Controller"),
                        vid=data.get("VID", ""),
                        pid=data.get("PID", ""),
                        instance_id=data.get("InstanceId", ""),
                        serial=data.get("Serial", ""),
                    )
                    controllers.append(ctrl)
                except json.JSONDecodeError:
                    continue
        
        if not controllers:
            logger.info("PowerShell detection found no controllers, trying fallback...")
            controllers = detect_controllers_fallback()
            
    except subprocess.TimeoutExpired:
        logger.error("PowerShell controller detection timed out")
        controllers = detect_controllers_fallback()
    except Exception as e:
        logger.error(f"Controller detection error: {e}")
        controllers = detect_controllers_fallback()
    
    return controllers


def detect_controllers_fallback() -> List[ControllerInfo]:
    """Fallback controller detection using simple registry/devcon approach."""
    controllers = []
    
    try:
        # Use pnputil to list devices
        result = subprocess.run(
            ["pnputil", "/enum-devices", "/connected"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        
        if result.returncode == 0:
            current_device = {}
            for line in result.stdout.split('\n'):
                line = line.strip()
                if line.startswith("Instance ID:"):
                    if current_device and is_game_controller(current_device):
                        ctrl = parse_pnp_device(current_device)
                        if ctrl:
                            controllers.append(ctrl)
                    current_device = {"InstanceId": line.split(":", 1)[1].strip()}
                elif ":" in line and current_device:
                    key, val = line.split(":", 1)
                    current_device[key.strip()] = val.strip()
            
            # Don't forget the last device
            if current_device and is_game_controller(current_device):
                ctrl = parse_pnp_device(current_device)
                if ctrl:
                    controllers.append(ctrl)
    
    except Exception as e:
        logger.error(f"Fallback detection error: {e}")
    
    return controllers


def is_game_controller(device_info: dict) -> bool:
    """Check if a PnP device looks like a game controller."""
    text = " ".join(str(v) for v in device_info.values()).lower()
    keywords = ["controller", "gamepad", "joystick", "arcade", "xbox", 
                "playstation", "xinput", "game", "brooks", "brook", 
                "encoder", "hid_device_system_game", "ig_"]
    return any(kw in text for kw in keywords)


def parse_pnp_device(device_info: dict) -> Optional[ControllerInfo]:
    """Parse a PnP device entry into a ControllerInfo."""
    instance_id = device_info.get("InstanceId", "")
    name = device_info.get("Device Description", 
           device_info.get("Name", "Unknown Controller"))
    
    vid = pid = serial = ""
    vid_match = re.search(r"VID_([0-9A-Fa-f]{4})", instance_id)
    pid_match = re.search(r"PID_([0-9A-Fa-f]{4})", instance_id)
    
    if vid_match:
        vid = vid_match.group(1).upper()
    if pid_match:
        pid = pid_match.group(1).upper()
    
    parts = instance_id.split("\\")
    if len(parts) >= 3:
        serial = parts[-1]
    
    return ControllerInfo(
        device_name=name,
        vid=vid,
        pid=pid,
        instance_id=instance_id,
        serial=serial,
    )


# ─── Config File Operations ─────────────────────────────────────────

def read_retrostick_config(config_path: Path) -> List[PlayerAssignment]:
    """Read saved player assignments from RetroStick config file."""
    assignments = []
    
    if not config_path.exists():
        return assignments
    
    try:
        with open(config_path, 'r') as f:
            data = json.load(f)
        
        for entry in data.get("assignments", []):
            assignments.append(PlayerAssignment.from_dict(entry))
    except Exception as e:
        logger.error(f"Error reading config: {e}")
    
    return assignments


def write_retrostick_config(config_path: Path, assignments: List[PlayerAssignment],
                            retrobat_path: str = "", target_frontends: List[str] = None):
    """Write player assignments to RetroStick config file."""
    if target_frontends is None:
        target_frontends = ["retrobat"]
    
    data = {
        "version": "2.0",
        "retrobat_path": str(retrobat_path),
        "target_frontends": target_frontends,
        "assignments": [a.to_dict() for a in assignments]
    }
    
    config_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(config_path, 'w') as f:
        json.dump(data, f, indent=2)
    
    logger.info(f"Saved config to {config_path}")


def apply_to_retrobat(assignments: List[PlayerAssignment], retrobat_path: Path) -> bool:
    """Apply player assignments to RetroBat's EmulationStation config.
    
    This modifies es_input.cfg to ensure the controller order matches
    the saved player assignments. EmulationStation uses SDL GUIDs and
    device names to identify controllers, and the order of inputConfig
    entries in es_input.cfg determines the default player assignment.
    
    Additionally, we can write to es_settings.cfg to set explicit
    player-controller mappings.
    """
    success = True
    
    # === Update es_input.cfg ===
    es_input_path = get_es_input_cfg_path(retrobat_path)
    
    if es_input_path.exists():
        try:
            tree = ET.parse(es_input_path)
            root = tree.getroot()
            
            # Read existing joystick configs
            joystick_configs = []
            keyboard_configs = []
            
            for config in root.findall("inputConfig"):
                config_type = config.get("type", "")
                if config_type == "keyboard":
                    keyboard_configs.append(config)
                elif config_type == "joystick":
                    joystick_configs.append(config)
            
            # Match detected controllers to existing configs and reorder
            reordered = reorder_input_configs(joystick_configs, assignments)
            
            if reordered:
                # Tag deviceName with player number for in-menu identification
                sorted_assigns = sorted(assignments, key=lambda a: a.player_number)
                for idx, config in enumerate(reordered):
                    if idx < len(sorted_assigns):
                        pnum = sorted_assigns[idx].player_number
                        original_name = config.get("deviceName", "")
                        clean_name = re.sub(r'_Player\d+$', '', original_name)
                        config.set("deviceName", f"{clean_name}_Player{pnum}")

                # Rebuild the XML with keyboard first, then reordered joysticks
                root.clear()
                root.tag = "inputList"
                
                for kb in keyboard_configs:
                    root.append(kb)
                
                for joy in reordered:
                    root.append(joy)
                
                # Write back
                write_xml_pretty(tree, es_input_path)
                logger.info(f"Updated {es_input_path}")
            
        except ET.ParseError as e:
            logger.error(f"Failed to parse es_input.cfg: {e}")
            success = False
        except Exception as e:
            logger.error(f"Error updating es_input.cfg: {e}")
            success = False
    else:
        logger.warning(f"es_input.cfg not found at {es_input_path}")
    
    # === Update es_settings.cfg for player index overrides ===
    es_settings_path = get_es_settings_cfg_path(retrobat_path)
    
    if es_settings_path.exists():
        try:
            update_es_settings_players(es_settings_path, assignments)
            logger.info(f"Updated {es_settings_path}")
        except Exception as e:
            logger.error(f"Error updating es_settings.cfg: {e}")
            success = False
    
    return success


def reorder_input_configs(joystick_configs: list, 
                          assignments: List[PlayerAssignment]) -> list:
    """Reorder joystick inputConfig XML elements to match player assignments.
    
    The order of inputConfig entries in es_input.cfg determines which
    controller becomes P1, P2, etc. by default.
    """
    if not assignments or not joystick_configs:
        return joystick_configs
    
    # Build a lookup of existing configs
    remaining = list(joystick_configs)
    ordered = []
    
    for assignment in sorted(assignments, key=lambda a: a.player_number):
        ctrl = assignment.controller
        best_match = None
        best_score = 0
        
        for config in remaining:
            config_name = config.get("deviceName", "")
            config_guid = config.get("deviceGUID", "")
            config_path = config.get("devicePath", "")
            
            score = 0
            
            # Check device path match (includes VID/PID/instance)
            if config_path and ctrl.instance_id:
                if ctrl.instance_id.upper() in config_path.upper():
                    score = 100  # Best match
            
            # Check SDL GUID match
            if config_guid and ctrl.sdl_guid:
                if config_guid.lower() == ctrl.sdl_guid.lower():
                    score = max(score, 50)
            
            # Check device name match (strip any _PlayerN suffix)
            if config_name and ctrl.device_name:
                clean_name = re.sub(r'_Player\d+$', '', config_name)
                if clean_name.lower() == ctrl.device_name.lower():
                    score = max(score, 30)
            
            # Check VID/PID in GUID or path
            if ctrl.vid and ctrl.pid:
                vid_pid = f"{ctrl.vid}{ctrl.pid}".lower()
                if vid_pid in config_guid.lower() or vid_pid in config_path.lower():
                    score = max(score, 40)
            
            if score > best_score:
                best_score = score
                best_match = config
        
        if best_match is not None:
            ordered.append(best_match)
            remaining.remove(best_match)
    
    # Append any unmatched configs at the end
    ordered.extend(remaining)
    
    return ordered


def _xml_escape(text: str) -> str:
    """Escape special characters for XML attribute values."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def update_es_settings_players(settings_path: Path,
                               assignments: List[PlayerAssignment]):
    """Update es_settings.cfg with player-controller index mappings.

    EmulationStation can have explicit player-to-controller index
    settings that override the default ordering.

    RetroBat uses the format: INPUT P1NAME, INPUT P1GUID, INPUT P1PATH
    (no space between the player number and field name).
    """
    if not settings_path.exists():
        return

    lines = settings_path.read_text(encoding='utf-8').splitlines()
    new_lines = []

    # Remove existing INPUT P* NAME/GUID/PATH lines in BOTH formats:
    #   "INPUT P1NAME"  (RetroBat native - no space)
    #   "INPUT P1 NAME" (with space)
    player_setting_pattern = re.compile(
        r'^\s*<string\s+name="INPUT\s+P\d+\s*(NAME|GUID|PATH)"'
    )

    for line in lines:
        if not player_setting_pattern.match(line):
            new_lines.append(line)

    # Find insertion point (before </config> or at end)
    insert_idx = len(new_lines)
    for i in range(len(new_lines) - 1, -1, -1):
        if "</config>" in new_lines[i]:
            insert_idx = i
            break

    # Insert player assignment settings using RetroBat's native format
    for assignment in sorted(assignments, key=lambda a: a.player_number):
        ctrl = assignment.controller
        p = assignment.player_number

        if ctrl.device_name:
            clean_name = re.sub(r'_Player\d+$', '', ctrl.device_name)
            tagged_name = _xml_escape(f"{clean_name}_Player{p}")
            new_lines.insert(insert_idx,
                f'  <string name="INPUT P{p}NAME" value="{tagged_name}" />')
            insert_idx += 1

        if ctrl.sdl_guid:
            new_lines.insert(insert_idx,
                f'  <string name="INPUT P{p}GUID" value="{ctrl.sdl_guid}" />')
            insert_idx += 1

        if ctrl.instance_id:
            escaped_path = _xml_escape(ctrl.instance_id)
            new_lines.insert(insert_idx,
                f'  <string name="INPUT P{p}PATH" value="{escaped_path}" />')
            insert_idx += 1

    settings_path.write_text('\n'.join(new_lines), encoding='utf-8')


def write_xml_pretty(tree: ET.ElementTree, path: Path):
    """Write XML tree to file with nice formatting."""
    rough_string = ET.tostring(tree.getroot(), encoding='unicode')
    reparsed = minidom.parseString(rough_string)
    pretty = reparsed.toprettyxml(indent="  ", encoding=None)
    
    # Remove extra XML declaration if present
    lines = pretty.split('\n')
    if lines and lines[0].startswith('<?xml'):
        lines[0] = '<?xml version="1.0"?>'
    
    # Remove excessive blank lines
    cleaned = '\n'.join(line for line in lines if line.strip())
    
    path.write_text(cleaned, encoding='utf-8')


def apply_to_launchbox(assignments: List[PlayerAssignment],
                       launchbox_path: Path) -> bool:
    """Apply player assignments to LaunchBox/BigBox via RetroArch config.

    LaunchBox typically uses RetroArch for emulation. We modify retroarch.cfg
    to set input_playerN_joypad_index values that map controllers to
    the correct player slots.
    """
    retroarch_cfg = get_retroarch_cfg_path(launchbox_path)

    if retroarch_cfg is None:
        logger.warning("RetroArch config not found within LaunchBox installation")
        return False

    try:
        lines = retroarch_cfg.read_text(encoding='utf-8').splitlines()

        # Remove existing player joypad index settings
        player_index_pattern = re.compile(
            r'^\s*input_player\d+_joypad_index\s*=')
        lines = [l for l in lines if not player_index_pattern.match(l)]

        # Remove any previous RetroStick comment block
        lines = [l for l in lines
                 if l.strip() != "# RetroStick Fix - Player controller assignments"]

        # Add player joypad index assignments
        sorted_assignments = sorted(assignments, key=lambda a: a.player_number)
        lines.append("")
        lines.append("# RetroStick Fix - Player controller assignments")
        for assignment in sorted_assignments:
            p = assignment.player_number
            ctrl = assignment.controller
            idx = ctrl.xinput_index if ctrl.xinput_index >= 0 else (p - 1)
            lines.append(f'input_player{p}_joypad_index = "{idx}"')

        retroarch_cfg.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        logger.info(f"Updated RetroArch config at {retroarch_cfg}")
        return True

    except Exception as e:
        logger.error(f"Error updating RetroArch config: {e}")
        return False


# ─── Startup Application Logic ──────────────────────────────────────

def run_startup_fix(config_path: Path) -> bool:
    """Run the startup controller fix.
    
    This is the main function called at boot/before RetroBat launch.
    It reads the saved assignments, detects current controllers,
    and applies the correct ordering.
    """
    logger.info("RetroStick Fix - Starting controller assignment fix...")
    
    # Load saved config
    if not config_path.exists():
        logger.error(f"No config found at {config_path}. Run setup first.")
        return False
    
    with open(config_path, 'r') as f:
        config = json.load(f)
    
    retrobat_path = Path(config.get("retrobat_path", ""))
    if not retrobat_path.exists():
        logger.error(f"RetroBat path not found: {retrobat_path}")
        return False
    
    assignments = [PlayerAssignment.from_dict(a) for a in config.get("assignments", [])]
    if not assignments:
        logger.warning("No player assignments configured. Nothing to do.")
        return True
    
    # Detect currently connected controllers
    detected = detect_controllers_wmi()
    logger.info(f"Detected {len(detected)} controller(s):")
    for ctrl in detected:
        logger.info(f"  - {ctrl.display_name} ({ctrl.instance_id})")
    
    # Match detected controllers to saved assignments
    matched_assignments = []
    for assignment in assignments:
        saved_ctrl = assignment.controller
        for detected_ctrl in detected:
            if saved_ctrl.matches(detected_ctrl):
                # Update with current runtime info
                updated = ControllerInfo(
                    device_name=detected_ctrl.device_name or saved_ctrl.device_name,
                    vid=detected_ctrl.vid or saved_ctrl.vid,
                    pid=detected_ctrl.pid or saved_ctrl.pid,
                    instance_id=detected_ctrl.instance_id or saved_ctrl.instance_id,
                    serial=detected_ctrl.serial or saved_ctrl.serial,
                    sdl_guid=saved_ctrl.sdl_guid,  # Keep saved GUID
                    device_path=detected_ctrl.device_path or saved_ctrl.device_path,
                )
                matched_assignments.append(PlayerAssignment(
                    player_number=assignment.player_number,
                    controller=updated
                ))
                logger.info(f"  Matched P{assignment.player_number}: {saved_ctrl.display_name}")
                break
        else:
            logger.warning(f"  P{assignment.player_number} controller not found! "
                          f"({saved_ctrl.display_name})")
            matched_assignments.append(assignment)
    
    # Apply to frontends
    targets = config.get("target_frontends", ["retrobat"])
    success = True

    if "retrobat" in targets:
        rb_ok = apply_to_retrobat(matched_assignments, retrobat_path)
        if rb_ok:
            logger.info("Successfully applied controller assignments to RetroBat!")
        else:
            logger.error("Failed to apply some controller assignments to RetroBat")
            success = False

    if "launchbox" in targets:
        lb_ok = apply_to_launchbox(matched_assignments, retrobat_path)
        if lb_ok:
            logger.info("Successfully applied controller assignments to LaunchBox!")
        else:
            logger.error("Failed to apply controller assignments to LaunchBox")
            success = False

    if success:
        logger.info("Controller assignment fix complete!")
    return success
