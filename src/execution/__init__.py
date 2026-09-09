"""Paper execution: translates a sized `Signal` into an `Order`, fills
it, and tracks the resulting portfolio.

Closes the loop `DECISIONS.md` ADR-0021 (`src/risk`) left open --
`PaperBroker.account_state` is a real `AccountState` the next
`PositionSizer.size()` call can consume directly. Deliberately simple
this round: market orders only, instant full fills, no live
mark-to-market, one open position per symbol at a time. Real broker
connectivity is Sprint 5. See `DECISIONS.md`, ADR-0022.
"""

from src.execution.engine import PaperBroker
from src.execution.models import Fill, Order, OrderSide, Position

__all__ = ["PaperBroker", "Order", "OrderSide", "Fill", "Position"]
