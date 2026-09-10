"""Data shapes for broker order management (src/broker).

Plain dataclasses, no behavior beyond input validation -- `AlpacaBroker`
(`alpaca.py`) does the actual submission/parsing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OrderSide(Enum):
    """Which way an order moves cash and position.

    Deliberately a separate enum from `src.execution.models.OrderSide`,
    even though the values are identical today. `src/broker` is meant
    to stay independent of `src/execution` -- a broker is the more
    foundational capability a future execution layer builds on, not
    the other way around (see the dependency note in
    `ARCHITECTURE.md`). Duplicating a two-value enum costs nothing;
    importing across that boundary would.
    """

    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    """Where a submitted order stands, per the broker's own bookkeeping.

    A real order doesn't fill instantly and completely the way
    `PaperBroker`'s does -- this is exactly the asynchronous lifecycle
    `DECISIONS.md` ADR-0022/ADR-0023 flagged as the reason order
    submission needed its own design round (ADR-0024).
    """

    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"


@dataclass
class OrderRequest:
    """An instruction to trade `quantity` units of `symbol`.

    Market order only -- no limit price, no order type -- matching
    `src/execution`'s own simplicity (ADR-0022).

    Args:
        symbol: which instrument to trade.
        side: `BUY` or `SELL`.
        quantity: units to trade. Always positive.

    Raises:
        ValueError: `quantity` isn't positive.
    """

    symbol: str
    side: OrderSide
    quantity: float

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")


@dataclass
class BrokerOrder:
    """The broker's own record of a submitted order.

    Its `status` may change after this is returned -- call
    `BrokerConnection.get_order()` again to check.

    Args:
        broker_order_id: the broker's own identifier for this order --
            not a `Signal.id` or anything from this platform's own
            models, just whatever string the broker itself assigned.
        symbol: which instrument.
        side: `BUY` or `SELL`.
        quantity: units requested.
        status: where the order currently stands.
        filled_quantity: units actually filled so far (`0.0` until at
            least a partial fill).
        filled_avg_price: the average price filled at so far, or
            `None` if nothing has filled yet.
    """

    broker_order_id: str
    symbol: str
    side: OrderSide
    quantity: float
    status: OrderStatus
    filled_quantity: float = 0.0
    filled_avg_price: float | None = None
