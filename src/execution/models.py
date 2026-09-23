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
    """The result of executing an `Order` -- by `PaperBroker` (instant,
    caller-supplied price, no cost modeling, unchanged since ADR-0022)
    or by `src.backtesting.execution_model.ExecutionModel` (Sprint 12,
    `DECISIONS.md` ADR-0045: candle-level timing, slippage, and fees).
    One shape for both, so nothing downstream (`apply_fill_to_portfolio`,
    analytics) needs to know which produced a given `Fill`.

    Args:
        order: the `Order` that was filled.
        fill_price: the price it was actually filled at -- after
            slippage, when an `ExecutionModel` produced this `Fill`;
            identical to `reference_price` when none was applied (e.g.
            every `PaperBroker` fill, or a zero-slippage
            `ExecutionModel` configuration).
        cash_delta: the resulting change in cash -- negative for a
            `BUY` (cash paid out), positive for a `SELL` (cash received
            in), regardless of whether that `SELL` was opening a short
            or closing a long. Includes `fee` when one was applied
            (Sprint 12 spec, section 14: fees enter the fill's economic
            effect exactly once, here).
        reference_price: the frictionless price this fill was priced
            from, before slippage (Sprint 12, `DECISIONS.md` ADR-0045)
            -- `None` for a `PaperBroker` fill (no execution model is
            involved at all) or when a `Fill` predates this field.
            Equal to `fill_price` under a zero-slippage configuration.
        fill_timestamp: when this fill actually happened -- may differ
            from `order.timestamp` (the originating signal's own
            timestamp) under a realistic execution timing convention
            (e.g. `ExecutionTiming.NEXT_BAR_OPEN`). `None` when not
            tracked (every `PaperBroker` fill, or a `Fill` predating
            this field) -- never assumed equal to `order.timestamp`.
        slippage_amount: `fill_price - reference_price`, signed --
            positive for a `BUY` (paid more), negative for a `SELL`
            (received less), `0.0` under a zero-slippage configuration
            or when no execution model produced this fill.
        fee: the dollar fee charged for this fill (`0.0` by default --
            every `PaperBroker` fill and every zero-fee `ExecutionModel`
            configuration). Already reflected in `cash_delta`; recorded
            here separately purely so it stays individually measurable
            (Sprint 12 spec, section 17) rather than only visible as
            part of the combined cash effect.
    """

    order: Order
    fill_price: float
    cash_delta: float
    reference_price: float | None = None
    fill_timestamp: pd.Timestamp | None = None
    slippage_amount: float = 0.0
    fee: float = 0.0


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
