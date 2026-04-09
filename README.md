# 🕹️ RetroStick by Fnordcorps

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/fnordcorps)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows%2010%2F11-blue.svg)]()
[![Python 3.8+](https://img.shields.io/badge/Python-3.8%2B-green.svg)]()

**Persistent arcade controller assignment for retro gaming cabinets on Windows**

---

## The Problem

If you've built a retro arcade cabinet running **RetroBat**, **LaunchBox**, or any EmulationStation-based frontend on Windows, you've probably hit this issue:

> **Every time you power on the cabinet, Windows randomly shuffles which USB controller is Player 1 and which is Player 2.**

This happens because Windows does not guarantee the enumeration order of USB devices across reboots. Your left-side joystick (Player 1) might suddenly become Player 2, and vice versa. Since the cabinet is sealed up, you can't just unplug and re-plug the controllers.

This is an extremely common problem in the arcade cabinet building community, affecting anyone using USB encoder boards like **Brooks Universal**, **Zero Delay encoders**, **I-PAC**, **J-PAC**, **X-Arcade**, or similar hardware.

## The Solution

**RetroStick Fix** solves this by:

1. **Identifying controllers by persistent hardware IDs** — USB devices have hardcoded Vendor ID (VID), Product ID (PID), and instance-specific identifiers (serial numbers or port paths) that survive reboots
2. **Mapping those IDs to player positions** — You tell it once which physical controller is P1, P2, P3, P4
3. **Fixing the config on every startup** — A lightweight script runs before your frontend launches and writes the correct controller order to the configuration files

### Supported Frontends

| Frontend | Status | Config Modified |
|----------|--------|----------------|
| **RetroBat** | ✅ Full support | `es_input.cfg`, `es_settings.cfg` |
| **LaunchBox / BigBox** | 🔄 Planned | Controller order settings |
| **EmulationStation (standalone)** | ✅ Works | `es_input.cfg` |
| **Batocera (Windows)** | 🔄 Planned | `batocera.conf` |

## How It Works

```
                    ┌─────────────────────────────────┐
                    │       ARCADE CABINET             │
                    │                                  │
  ┌──────────┐      │  ┌──────────┐   ┌──────────┐   │
  │ P1 Stick │──USB──│──│ Brooks   │   │ Brooks   │──USB──│── P2 Stick
  │ + Buttons│      │  │ Board #1 │   │ Board #2 │   │    │ + Buttons
  └──────────┘      │  │ VID:0F0D │   │ VID:0F0D │   │    └──────────┘
                    │  │ SN:A1B2  │   │ SN:C3D4  │   │
                    │  └────┬─────┘   └────┬─────┘   │
                    │       │              │          │
                    │       └──────┬───────┘          │
                    │              │                   │
                    │         ┌────▼────┐              │
                    │         │   PC    │              │
                    │         │ Windows │              │
                    │         └────┬────┘              │
                    │              │                   │
                    │    ┌─────────▼──────────┐        │
                    │    │  RetroStick Fix    │        │
                    │    │  (runs at startup) │        │
                    │    │                    │        │
                    │    │ 1. Read saved map  │        │
                    │    │ 2. Detect by VID/  │        │
                    │    │    PID/Serial      │        │
                    │    │ 3. Write correct   │        │
                    │    │    order to config │        │
                    │    └─────────┬──────────┘        │
                    │              │                   │
                    │    ┌─────────▼──────────┐        │
                    │    │     RetroBat /     │        │
                    │    │   EmulationStation │        │
                    │    │                    │        │
                    │    │  P1 = Board #1 ✓  │        │
                    │    │  P2 = Board #2 ✓  │        │
                    │    └───────────────────┘         │
                    └─────────────────────────────────┘
```

### Technical Details

Windows assigns each USB device a **Device Instance Path** that looks like:
```
USB\VID_0F0D&PID_00C1&IG_00\7&12345678&0&0000
```

This contains:
- **VID** (Vendor ID) — identifies the manufacturer (hardcoded in firmware)
- **PID** (Product ID) — identifies the specific product model (hardcoded in firmware)
- **Instance ID** — unique to this specific physical device (based on serial number or USB port topology)

RetroStick Fix uses these identifiers to reliably tell your controllers apart, even when Windows enumerates them in a different order after a reboot.

## Installation

### Prerequisites

- **Windows 10 or 11**
- **Python 3.8+** (if running from source)
- **RetroBat** (or another supported frontend) installed
- USB game controllers / encoder boards connected

### From Source

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/retrostick-fix.git
cd retrostick-fix

# No external dependencies required! Uses only Python standard library.

# Run the GUI
python retrostick_gui.py
```

### Compiled Release

Pre-built `.exe` files available on the [Releases](../../releases) page so you don't need Python installed.

## Usage

### 1. First-Time Setup (GUI)

Run the setup GUI:
```bash
python retrostick_gui.py
```

1. **Set RetroBat Path** — The app auto-detects common locations. Browse if needed.
2. **Set Player Count** — Select how many players your cabinet supports (1-4).
3. **Click "Setup Controllers"** — The app detects all connected game controllers.
4. **Assign each player** — For each player position, select the corresponding controller from the list. The hardware ID helps you verify which physical device is which.
5. **Click "Apply & Save"** — Writes the config and updates RetroBat.
6. **Click "Install Startup Task"** — Adds a script to Windows Startup so it runs automatically on every boot.

### 2. Automatic Startup Fix

Once installed, the fix runs silently every time Windows starts:

```
Windows boots → RetroStick Fix runs → Detects controllers → 
Writes correct order → RetroBat launches with correct P1/P2/P3/P4
```

### 3. Manual Run

You can also run the fix manually (headless, no GUI):
```bash
python retrostick_startup.py
```

Or with verbose logging:
```bash
python retrostick_startup.py --verbose
```

### 4. Integration with RetroBat Events

RetroBat supports running scripts on certain events. You can add RetroStick Fix to run before EmulationStation starts:

1. Navigate to your RetroBat installation folder
2. Go to `emulationstation\.emulationstation\scripts\system-selected\`  
   (create the folders if they don't exist)
3. Place a shortcut or `.bat` file that runs `retrostick_startup.py`

## Configuration

The configuration is stored at:
```
%USERPROFILE%\.retrostick-fix\retrostick_config.json
```

Example config:
```json
{
  "version": "1.0",
  "retrobat_path": "C:\\RetroBat",
  "target_frontends": ["retrobat"],
  "assignments": [
    {
      "player_number": 1,
      "controller": {
        "device_name": "Brook Universal Fighting Board",
        "vid": "0F0D",
        "pid": "00C1",
        "instance_id": "USB\\VID_0F0D&PID_00C1\\A1B2C3D4",
        "serial": "A1B2C3D4",
        "sdl_guid": "",
        "xinput_index": 0,
        "device_path": ""
      }
    },
    {
      "player_number": 2,
      "controller": {
        "device_name": "Brook Universal Fighting Board",
        "vid": "0F0D",
        "pid": "00C1",
        "instance_id": "USB\\VID_0F0D&PID_00C1\\E5F6G7H8",
        "serial": "E5F6G7H8",
        "sdl_guid": "",
        "xinput_index": 1,
        "device_path": ""
      }
    }
  ]
}
```

Logs are written to:
```
%USERPROFILE%\.retrostick-fix\retrostick.log
```

## Identifying Your Controllers

If you're not sure which controller is which, here are some tips:

### Method 1: Windows Device Manager
1. Open Device Manager (Win+X → Device Manager)
2. Expand "Xbox Peripherals" or "Human Interface Devices"
3. Right-click each controller → Properties → Details → Device Instance Path
4. Unplug one controller to see which one disappears

### Method 2: Windows Game Controllers
1. Press Win+R, type `joy.cpl`, hit Enter
2. This shows all connected game controllers
3. Select one and click Properties to see which buttons light up when you press them

### Method 3: Use RetroStick Fix
The setup GUI detects all controllers and shows their hardware IDs. The names and VID/PID values help identify which is which.

## Troubleshooting

### Controllers not detected
- Make sure controllers are plugged in and recognized by Windows
- Check Device Manager for any yellow warning icons
- Try running the app as Administrator
- Some encoder boards need drivers installed first

### Same VID/PID for all controllers
This is normal for identical encoder boards (e.g. two Brooks boards). The **instance ID** portion (serial number or port path) differentiates them. As long as they're plugged into the same USB ports, the instance IDs stay consistent.

### Config not taking effect
- Make sure RetroBat is **not running** when you apply the config
- Check that the RetroBat path is correct
- Look at the log file for error messages
- Verify `es_input.cfg` exists in your RetroBat installation

### Controllers still swap after reboot
- Verify the startup script is running (check Task Manager → Startup tab)
- Try running `retrostick_startup.py --verbose` manually and check the output
- If controllers are on a USB hub, try connecting directly to motherboard ports
- USB port topology changes (plugging into different ports) will change instance IDs

## How This Differs From Other Solutions

| Solution | Limitation | RetroStick Fix |
|----------|-----------|----------------|
| **devreorder** | DirectInput only, no XInput | Works with XInput (what RetroBat uses) |
| **x360ce** | Hooks into game DLLs, can cause instability | Modifies config files, no DLL injection |
| **DS4Windows** | PlayStation controllers only | Works with any USB controller |
| **Manually re-plugging** | Requires opening the cabinet | Automatic, no physical access needed |
| **RetroBat built-in** | Doesn't persist across reboots | Persistent across reboots |


### Building from Source

```bash
# Clone
git clone https://github.com/fnordcorps/retrostick-fix.git
cd retrostick-fix

# Run tests (if any)
python -m pytest tests/

# Build standalone exe (requires PyInstaller)
pip install pyinstaller
pyinstaller --onefile --windowed --name RetroStickFix retrostick_gui.py
```

## License

MIT License — See [LICENSE](LICENSE) for details.

## Acknowledgements

- The **RetroBat** team for their incredible frontend
- The **EmulationStation** project
- The arcade cabinet building community for building awesome cabs
- **devreorder** for inspiration on the approach (though we solve it differently)

---

*Made with ❤️ for the arcade cabinet community. No more swapped controllers!*
