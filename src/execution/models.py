"""Data shapes for paper execution (src/execution).

Plain dataclasses, no behavior beyond input validation -- `PaperBroker`
(`engine.py`) does the actual bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

import pandas as pd


class OrderSide(Enum):
    """Which way an order moves cash and position.

    Distinct from `SignalDirection` (`LONG`/`SHORT`/`FLAT`): a side is
    what an order *does* (buy or sell units), not what a strategy
    *wants* (a target position) -- `PaperBroker` is what translates one
    into the other, since the mapping depends on whether a position is
    being opened or closed.
    """

    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Order:
    """An instruction to trade `quantity` units of `symbol`, translated
    from a `Signal` by `PaperBroker`. Market order only -- no limit
    price, no order type -- matching this platform's "deliberately
    simple first" posture for execution (`DECISIONS.md`, ADR-0022).

    Args:
        symbol: which instrument this order is for.
        side: `BUY` or `SELL`.
        quantity: units to trade. Always positive -- direction lives in
            `side`, not the sign of `quantity`.
        signal_id: the `Signal.id` this order was translated from,
            preserving traceability the same way
            `Trade.entry_signal_id` does in `src/backtesting`.
        timestamp: when the originating signal was generated.

    Raises:
        ValueError: `quantity` isn't positive.
    """

    symbol: str
    side: OrderSide
    quantity: float
    signal_id: UUID
    timestamp: pd.Timestamp

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")


@dataclass
class Fill:
    """The result of `PaperBroker` executing an `Order`.

    Args:
        order: the `Order` that was filled.
        fill_price: the price it was filled at.
        cash_delta: the resulting change in the broker's cash --
            negative for a `BUY` (cash paid out), positive for a `SELL`
            (cash received in), regardless of whether that `SELL` was
            opening a short or closing a long.
    """

    order: Order
    fill_price: float
    cash_delta: float


@dataclass
class Position:
    """One open position `PaperBroker` is currently holding.

    Args:
        symbol: which instrument.
        quantity: signed -- positive is long, negative is short.
        entry_price: the price this position was opened at. Used as a
            frozen valuation for `PaperBroker.account_state` -- this
            isn't marked to market, since the broker has no ongoing
            price feed (see `engine.py`'s module docstring).
        entry_signal_id: the `Signal.id` that opened this position.
    """

    symbol: str
    quantity: float
    entry_price: float
    entry_signal_id: UUID
