"""Central settings for the AI Trading Platform.

Every path and every piece of market configuration the application needs
lives here. If a new module needs to know "where does X live" or "what
symbols do we track," it imports from this module -- it should never
construct its own paths or hardcode a watchlist.

Extending this later (e.g. loading overrides from a .env file or a YAML
config) means changing this one module; nothing that imports from it
needs to change.
"""

from pathlib import Path

# Project root: three levels up from this file
# (src/config/settings.py -> src/config -> src -> project root).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Directories
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
CONFIG_DIR = PROJECT_ROOT / "config"

# Create directories if missing
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# Market configuration
WATCHLIST = [
    "SPY",
    "QQQ",
]

DEFAULT_PERIOD = "2y"
DEFAULT_INTERVAL = "5m"
