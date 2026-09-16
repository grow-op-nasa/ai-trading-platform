"""Paper execution: translates a sized `Signal` into an `Order`, fills
it, and tracks the resulting portfolio.

Closes the loop `DECISIONS.md` ADR-0021 (`src/risk`) left open --
`PaperBroker.account_state` is a real `src.portfolio.AccountState` the
next `PositionSizer.size()` call can consume directly. Deliberately
simple this round: market orders only, instant full fills, no live
mark-to-market (`account_state.equity` values open positions at their
frozen entry price, not a current market price -- see `engine.py`'s
docstrings), one open position per symbol at a time. Real broker
connectivity is Sprint 5. See `DECISIONS.md`, ADR-0022.

Sprint 7 (`DECISIONS.md`, ADR-0039) adds `apply_fill_to_portfolio()` --
the glue that keeps a `src.portfolio.Portfolio` in sync with
`PaperBroker`'s `Fill`s, for callers that want the richer,
portfolio-aware risk layer alongside paper execution. `PaperBroker`
itself is unchanged.
"""

from src.execution.engine import PaperBroker
from src.execution.models import Fill, Order, OrderSide, Position
from src.execution.portfolio_sync import apply_fill_to_portfolio

__all__ = [
    "PaperBroker",
    "Order",
    "OrderSide",
    "Fill",
    "Position",
    "apply_fill_to_portfolio",
]
