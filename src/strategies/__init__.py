"""Trading strategies.

No concrete strategy exists yet (that's Sprint 3) -- but the interface
every strategy must satisfy is defined now, in `base.py`, since the
Sprint 2 Backtesting Framework needs something real to run against.
"""

from src.strategies.base import SIGNAL_COLUMN, Strategy

__all__ = ["Strategy", "SIGNAL_COLUMN"]
