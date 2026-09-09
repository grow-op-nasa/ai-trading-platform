"""Trading strategies.

The interface every strategy must satisfy is defined in `base.py`. As
of ADR-0015, strategies speak in `Signal` objects (`src/signals/`), not
raw DataFrame columns.

`sdk.py`'s `BaseStrategy` (ADR-0018) is an optional convenience --
handles indicator access, logging, column validation, and signal
construction -- but never decides when to emit a signal or what
direction to choose. A strategy can still implement `Strategy`
directly, with no base class at all, exactly as before.

`ema_cross.py`'s `EMACrossStrategy` is the platform's first permanent
strategy -- deliberately simple, built to exercise the platform end to
end rather than to be profitable as-is.
"""

from src.strategies.base import Strategy
from src.strategies.ema_cross import EMACrossStrategy
from src.strategies.sdk import BaseStrategy

__all__ = ["Strategy", "BaseStrategy", "EMACrossStrategy"]
