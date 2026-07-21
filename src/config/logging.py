"""Application-wide logging configuration.

Importing this module configures the shared `loguru` logger with two
sinks: a rotating log file and stdout. Every other module should import
`logger` from here rather than calling `loguru.logger` directly or
configuring its own sinks -- that keeps log formatting, rotation, and
retention consistent across the whole application.

Note: `log_path` is relative to the current working directory, so run
the application from the project root (as `python src/main.py`).
"""

from pathlib import Path

from loguru import logger

log_path = Path("logs/trading.log")

# Remove the default stderr sink so we control exactly what's configured.
logger.remove()

# File sink: rotates at 10 MB, keeps 30 days of history.
logger.add(
    log_path,
    rotation="10 MB",
    retention="30 days",
    level="INFO",
)

# Console sink: plain print, no loguru's default coloring/formatting
# wrapper, so output stays readable in any terminal.
logger.add(
    lambda msg: print(msg, end=""),
    level="INFO",
)
