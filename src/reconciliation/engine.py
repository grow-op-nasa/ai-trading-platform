"""Compare a `PaperBroker` simulated fill against a real broker order.

`PaperBroker` (`src/execution`) fills instantly, completely, and at
whatever price the caller supplies -- a deliberate simplification
(`DECISIONS.md`, ADR-0022). A real broker doesn't work that way: fills
happen at whatever price the market actually gave, sometimes only
partially. `reconcile_fill()` is the seam where those two worlds meet
and get compared -- see ADR-0027 for why this module is allowed to
depend on both `src.execution` and `src.broker`, unlike ADR-0024's rule
keeping `src.broker` independent of `src.execution`.
"""

from __future__ import annotations

from src.broker.models import BrokerOrder, OrderStatus
from src.execution.models import Fill, OrderSide
from src.reconciliation.models import FillReconciliation

_FILLED_STATUSES = (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)


def reconcile_fill(real_order: BrokerOrder, simulated_fill: Fill) -> FillReconciliation:
    """Compare `simulated_fill` (what `PaperBroker` assumed) against
    `real_order` (what actually happened at a real broker).

    Args:
        real_order: the real broker's record of the order, already
            fetched via `BrokerConnection.get_order()` (or returned
            from `submit_order()`) -- must be `FILLED` or
            `PARTIALLY_FILLED`.
        simulated_fill: the `Fill` `PaperBroker.submit_signal()`
            produced for the equivalent order.

    Raises:
        ValueError: `real_order` and `simulated_fill` are for different
            symbols or sides, or `real_order` hasn't actually filled
            (any) yet.
    """
    if real_order.symbol != simulated_fill.order.symbol:
        raise ValueError(
            f"cannot reconcile different symbols: real order is "
            f"{real_order.symbol!r}, simulated fill is "
            f"{simulated_fill.order.symbol!r}"
        )

    # OrderSide is deliberately two separate enums in src.broker and
    # src.execution (see DECISIONS.md, ADR-0024) -- compare by value.
    real_side = real_order.side.value
    simulated_side = simulated_fill.order.side.value
    if real_side != simulated_side:
        raise ValueError(
            f"cannot reconcile different sides: real order is "
            f"{real_side!r}, simulated fill is {simulated_side!r}"
        )

    if real_order.status not in _FILLED_STATUSES:
        raise ValueError(
            f"cannot reconcile an order that hasn't filled: real order "
            f"status is {real_order.status.value!r}"
        )

    if real_order.filled_avg_price is None:
        raise ValueError("real order has no filled_avg_price to reconcile against")

    simulated_price = simulated_fill.fill_price
    real_price = real_order.filled_avg_price

    # Side-normalized: positive always means "reality was worse than
    # the simulation assumed," regardless of BUY/SELL direction.
    if simulated_fill.order.side is OrderSide.BUY:
        price_slippage_per_share = real_price - simulated_price
    else:
        price_slippage_per_share = simulated_price - real_price

    price_slippage_pct = price_slippage_per_share / simulated_price

    simulated_quantity = simulated_fill.order.quantity
    real_filled_quantity = real_order.filled_quantity
    quantity_shortfall = simulated_quantity - real_filled_quantity

    cost_impact = price_slippage_per_share * real_filled_quantity

    return FillReconciliation(
        symbol=real_order.symbol,
        real_broker_order_id=real_order.broker_order_id,
        simulated_price=simulated_price,
        real_price=real_price,
        price_slippage_per_share=price_slippage_per_share,
        price_slippage_pct=price_slippage_pct,
        simulated_quantity=simulated_quantity,
        real_filled_quantity=real_filled_quantity,
        quantity_shortfall=quantity_shortfall,
        cost_impact=cost_impact,
    )
