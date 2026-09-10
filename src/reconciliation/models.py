"""Data shapes for fill reconciliation (src/reconciliation).

Plain dataclass, no behavior beyond what's constructed for it --
`engine.py`'s `reconcile_fill()` does the actual comparison.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FillReconciliation:
    """The result of comparing one simulated `PaperBroker` `Fill`
    against the real `BrokerOrder` it was meant to approximate.

    All price/cost fields are **side-normalized**: positive always
    means the real execution was worse than what `PaperBroker`
    assumed, negative always means it was better, regardless of
    whether the order was a `BUY` or a `SELL`. A `BUY` that filled at a
    higher real price than simulated paid more than assumed
    (positive); a `SELL` that filled at a lower real price than
    simulated received less than assumed (positive) -- both are the
    same kind of "reality was worse" outcome, even though the raw
    price difference would have opposite signs.

    Args:
        symbol: which instrument this comparison is for.
        real_broker_order_id: the real broker's order id, for
            traceability back to the actual order.
        simulated_price: the price `PaperBroker` was told to fill at.
        real_price: the real broker's `filled_avg_price`.
        price_slippage_per_share: side-normalized; `real_price` vs.
            `simulated_price`, per share.
        price_slippage_pct: `price_slippage_per_share` as a fraction
            of `simulated_price`.
        simulated_quantity: the quantity `PaperBroker` assumed filled
            completely.
        real_filled_quantity: the quantity the real broker actually
            filled -- may be less than `simulated_quantity` if the real
            order only partially filled.
        quantity_shortfall: `simulated_quantity - real_filled_quantity`
            -- positive means the real fill covered less size than
            `PaperBroker` assumed.
        cost_impact: `price_slippage_per_share * real_filled_quantity`
            -- the total dollar impact of the price difference, using
            the quantity that actually filled in reality. Side-
            normalized the same way `price_slippage_per_share` is:
            positive means the real execution cost more (or yielded
            less) than the simulation assumed.
    """

    symbol: str
    real_broker_order_id: str
    simulated_price: float
    real_price: float
    price_slippage_per_share: float
    price_slippage_pct: float
    simulated_quantity: float
    real_filled_quantity: float
    quantity_shortfall: float
    cost_impact: float
