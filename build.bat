@echo off
REM ============================================================
REM RetroStick Fix - Build Script
REM Creates standalone Windows executables using PyInstaller
REM ============================================================

echo.
echo  ╔══════════════════════════════════════╗
echo  ║   RetroStick Fix - Build Script      ║
echo  ╚══════════════════════════════════════╝
echo.

REM Check for Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.8+ first.
    pause
    exit /b 1
)

REM Check for PyInstaller
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
)

echo.
echo Building GUI application...
echo.

pyinstaller ^
    --onefile ^
    --windowed ^
    --name "RetroStickFix" ^
    --add-data "controller_core.py;." ^
    --add-data "retrostick_startup.py;." ^
    retrostick_gui.py

echo.
echo Building startup script (headless)...
echo.

pyinstaller ^
    --onefile ^
    --console ^
    --name "RetroStickFix_Startup" ^
    --add-data "controller_core.py;." ^
    retrostick_startup.py

echo.
echo ============================================================
echo Build complete!
echo.
echo Files created in the 'dist' folder:
echo   - RetroStickFix.exe        (GUI application)
echo   - RetroStickFix_Startup.exe (Startup/headless script)
echo.
echo Copy both files to the same folder on your arcade cabinet PC.
echo ============================================================
echo.
pause
