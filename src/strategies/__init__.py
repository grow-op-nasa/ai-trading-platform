"""Trading strategies.

No concrete strategy exists yet (Sprint 3's first strategy is
deliberately simple -- see `ROADMAP.md`) -- but the interface every
strategy must satisfy is defined now, in `base.py`. As of ADR-0015,
strategies speak in `Signal` objects (`src/signals/`), not raw
DataFrame columns.

`sdk.py`'s `BaseStrategy` (ADR-0018) is an optional convenience --
handles indicator access, logging, column validation, and signal
construction -- but never decides when to emit a signal or what
direction to choose. A strategy can still implement `Strategy`
directly, with no base class at all, exactly as before.
"""

from src.strategies.base import Strategy
from src.strategies.sdk import BaseStrategy

__all__ = ["Strategy", "BaseStrategy"]
