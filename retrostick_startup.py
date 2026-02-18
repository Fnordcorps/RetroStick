"""
RetroStick Fix - Startup Script
=================================
Lightweight script that runs at Windows startup (or before RetroBat)
to ensure arcade controllers are assigned to the correct players.

This script:
1. Reads the saved controller-to-player mappings
2. Detects currently connected controllers
3. Matches them by persistent hardware identifiers
4. Writes the correct ordering to RetroBat/EmulationStation config

Usage:
    python retrostick_startup.py
    python retrostick_startup.py --config "C:\\path\\to\\config.json"
"""

import sys
import os
import logging
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from controller_core import run_startup_fix

# Default config location
CONFIG_DIR = Path(os.path.expanduser("~")) / ".retrostick-fix"
CONFIG_FILE = CONFIG_DIR / "retrostick_config.json"
LOG_FILE = CONFIG_DIR / "retrostick.log"


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="RetroStick Fix - Startup Controller Assignment")
    parser.add_argument("--config", type=str, default=str(CONFIG_FILE),
                       help="Path to config file")
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="Enable verbose logging")
    args = parser.parse_args()
    
    # Setup logging
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, mode='a'),
            logging.StreamHandler(),
        ]
    )
    
    logger = logging.getLogger("retrostick")
    logger.info("=" * 60)
    logger.info("RetroStick Fix - Startup Controller Assignment")
    logger.info("=" * 60)
    
    config_path = Path(args.config)
    
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        logger.error("Please run the RetroStick Fix GUI to set up your controllers first.")
        sys.exit(1)
    
    success = run_startup_fix(config_path)
    
    if success:
        logger.info("Controller assignment fix completed successfully!")
        sys.exit(0)
    else:
        logger.error("Controller assignment fix encountered errors.")
        sys.exit(1)


if __name__ == "__main__":
    main()
