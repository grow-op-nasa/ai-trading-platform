"""Glue between `PaperBroker`'s `Fill`s and the neutral `Portfolio`
domain model -- Sprint 7 (`DECISIONS.md`, ADR-0039).

`src/portfolio` cannot depend on `src/execution` (`Fill`/`Order`
would cross the forbidden dependency direction, `tests/test_architecture.py`,
`test_portfolio_package_depends_on_nothing_else_in_this_codebase`), so
`Portfolio` itself only exposes `open_position()`/`close_position()` on
primitive terms. `src/execution` is already allowed to depend on
`src/portfolio` (it does, for `AccountState`) -- this module is the
small, additive translation layer living on the allowed side of that
boundary: it reads a `Fill` `PaperBroker` already produced and calls the
right `Portfolio` method. `PaperBroker` itself is not modified; a caller
that wants both a `PaperBroker` (execution simulation) and a `Portfolio`
(risk-facing domain state) applies each `Fill` to both, explicitly, via
this one function.
"""

from __future__ import annotations

from src.execution.models import Fill, OrderSide
from src.portfolio.models import Portfolio
from src.portfolio.position import Position, PositionSide


def apply_fill_to_portfolio(
    portfolio: Portfolio, fill: Fill, stop_price: float | None = None
) -> Position:
    """Apply `fill` to `portfolio`, opening or closing a position as
    appropriate, and return the resulting `Position`.

    Whether `fill` opens or closes is inferred the same way
    `PaperBroker._fill()` infers it: if `portfolio` has no open position
    in `fill.order.symbol`, this is an open; if it does, this is a
    close. `PaperBroker` only ever produces one or the other for a given
    symbol (one open position per symbol at a time, no scaling,
    `DECISIONS.md` ADR-0022) -- there is no third case to handle.

    Args:
        portfolio: the `Portfolio` to update, mutated in place.
        fill: the `Fill` `PaperBroker.submit_signal()` just produced.
        stop_price: the risk boundary this trade was sized against, if
            any (`src.risk.portfolio_risk.PortfolioRiskEngine`) --
            recorded on the opened `Position`. Ignored when `fill`
            closes a position; a closed position doesn't retain its
            original stop (`Position.close()` intentionally preserves
            it, but nothing further reads it once `CLOSED`).

    Returns:
        The `Position` that was opened, or the (now `CLOSED`) `Position`
        that was closed.
    """
    order = fill.order
    if portfolio.has_open_position(order.symbol):
        return portfolio.close_position(order.symbol, exit_price=fill.fill_price)

    side = PositionSide.LONG if order.side is OrderSide.BUY else PositionSide.SHORT
    signed_quantity = order.quantity if side is PositionSide.LONG else -order.quantity
    return portfolio.open_position(
        symbol=order.symbol,
        side=side,
        quantity=signed_quantity,
        entry_price=fill.fill_price,
        entry_timestamp=order.timestamp,
        entry_signal_id=order.signal_id,
        stop_price=stop_price,
    )
