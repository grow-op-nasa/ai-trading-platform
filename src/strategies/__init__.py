"""Trading strategies.

No concrete strategy exists yet (Sprint 3's first strategy is
deliberately simple -- see `ROADMAP.md`) -- but the interface every
strategy must satisfy is defined now, in `base.py`. As of ADR-0015,
strategies speak in `Signal` objects (`src/signals/`), not raw
DataFrame columns.
"""

from src.strategies.base import Strategy

__all__ = ["Strategy"]
