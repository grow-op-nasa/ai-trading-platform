"""Application entrypoint.

Run from the project root:

    python src/main.py

This file is intentionally thin: it wires together configuration and
logging and reports that the app started. As real components (market
data, strategies, execution) come online, they get called from here --
but this file should stay a thin orchestrator, not a place where logic
accumulates.
"""

from config.settings import WATCHLIST
from config.logging import logger

logger.info("AI Trading Platform Started")
logger.info(f"Watching: {WATCHLIST}")
