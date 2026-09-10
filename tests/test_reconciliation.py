"""Tests for fill reconciliation (src/reconciliation).

Built directly from `Fill`/`Order` (`src.execution.models`) and
`BrokerOrder` (`src.broker.models`) objects -- no `PaperBroker` or
`BrokerConnection` call involved, the same "construct the models
directly" posture `tests/test_execution.py` and `tests/test_risk.py`
already take.
"""

from __future__ import annotations

from uuid import uuid4

import pandas as pd
import pytest

from src.broker.models import BrokerOrder
from src.broker.models import OrderSide as BrokerOrderSide
from src.broker.models import OrderStatus
from src.execution.models import Fill, Order
from src.execution.models import OrderSide as ExecutionOrderSide
from src.reconciliation.engine import reconcile_fill
from src.reconciliation.models import FillReconciliation


def make_simulated_fill(
    symbol: str = "AAPL",
    side: ExecutionOrderSide = ExecutionOrderSide.BUY,
    quantity: float = 10.0,
    fill_price: float = 150.0,
    cash_delta: float = -1_500.0,
) -> Fill:
    order = Order(
        symbol=symbol,
        side=side,
        quantity=quantity,
        signal_id=uuid4(),
        timestamp=pd.Timestamp("2024-01-01"),
    )
    return Fill(order=order, fill_price=fill_price, cash_delta=cash_delta)


def make_real_order(
    symbol: str = "AAPL",
    side: BrokerOrderSide = BrokerOrderSide.BUY,
    quantity: float = 10.0,
    status: OrderStatus = OrderStatus.FILLED,
    filled_quantity: float = 10.0,
    filled_avg_price: float | None = 150.0,
) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id="order-123",
        symbol=symbol,
        side=side,
        quantity=quantity,
        status=status,
        filled_quantity=filled_quantity,
        filled_avg_price=filled_avg_price,
    )


# ---------------------------------------------------------------------------
# Exact match -- no slippage
# ---------------------------------------------------------------------------


def test_exact_match_has_zero_slippage_and_zero_shortfall():
    simulated = make_simulated_fill(fill_price=150.0, quantity=10.0)
    real = make_real_order(filled_avg_price=150.0, filled_quantity=10.0)

    result = reconcile_fill(real, simulated)

    assert isinstance(result, FillReconciliation)
    assert result.price_slippage_per_share == pytest.approx(0.0)
    assert result.price_slippage_pct == pytest.approx(0.0)
    assert result.quantity_shortfall == pytest.approx(0.0)
    assert result.cost_impact == pytest.approx(0.0)
    assert result.symbol == "AAPL"
    assert result.real_broker_order_id == "order-123"


# ---------------------------------------------------------------------------
# Price slippage -- sign normalization by side
# ---------------------------------------------------------------------------


def test_buy_filled_at_higher_real_price_is_positive_slippage():
    simulated = make_simulated_fill(side=ExecutionOrderSide.BUY, fill_price=150.0)
    real = make_real_order(side=BrokerOrderSide.BUY, filled_avg_price=151.50)

    result = reconcile_fill(real, simulated)

    assert result.price_slippage_per_share == pytest.approx(1.50)
    assert result.price_slippage_pct == pytest.approx(1.50 / 150.0)


def test_buy_filled_at_lower_real_price_is_negative_slippage():
    simulated = make_simulated_fill(side=ExecutionOrderSide.BUY, fill_price=150.0)
    real = make_real_order(side=BrokerOrderSide.BUY, filled_avg_price=148.0)

    result = reconcile_fill(real, simulated)

    assert result.price_slippage_per_share == pytest.approx(-2.0)


def test_sell_filled_at_lower_real_price_is_positive_slippage():
    simulated = make_simulated_fill(side=ExecutionOrderSide.SELL, fill_price=150.0)
    real = make_real_order(side=BrokerOrderSide.SELL, filled_avg_price=148.0)

    result = reconcile_fill(real, simulated)

    # SELL: receiving less than simulated is "worse" -> positive.
    assert result.price_slippage_per_share == pytest.approx(2.0)


def test_sell_filled_at_higher_real_price_is_negative_slippage():
    simulated = make_simulated_fill(side=ExecutionOrderSide.SELL, fill_price=150.0)
    real = make_real_order(side=BrokerOrderSide.SELL, filled_avg_price=152.0)

    result = reconcile_fill(real, simulated)

    assert result.price_slippage_per_share == pytest.approx(-2.0)


# ---------------------------------------------------------------------------
# Partial fills -- quantity shortfall
# ---------------------------------------------------------------------------


def test_partial_fill_reports_quantity_shortfall():
    simulated = make_simulated_fill(quantity=10.0)
    real = make_real_order(
        status=OrderStatus.PARTIALLY_FILLED, filled_quantity=6.0, filled_avg_price=150.0
    )

    result = reconcile_fill(real, simulated)

    assert result.simulated_quantity == pytest.approx(10.0)
    assert result.real_filled_quantity == pytest.approx(6.0)
    assert result.quantity_shortfall == pytest.approx(4.0)


def test_cost_impact_uses_real_filled_quantity():
    simulated = make_simulated_fill(side=ExecutionOrderSide.BUY, fill_price=150.0, quantity=10.0)
    real = make_real_order(
        side=BrokerOrderSide.BUY,
        status=OrderStatus.PARTIALLY_FILLED,
        filled_quantity=6.0,
        filled_avg_price=151.0,
    )

    result = reconcile_fill(real, simulated)

    # slippage_per_share = 1.0, applied to the 6 shares that actually filled.
    assert result.cost_impact == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_raises_on_symbol_mismatch():
    simulated = make_simulated_fill(symbol="AAPL")
    real = make_real_order(symbol="MSFT")

    with pytest.raises(ValueError):
        reconcile_fill(real, simulated)


def test_raises_on_side_mismatch():
    simulated = make_simulated_fill(side=ExecutionOrderSide.BUY)
    real = make_real_order(side=BrokerOrderSide.SELL)

    with pytest.raises(ValueError):
        reconcile_fill(real, simulated)


def test_raises_when_real_order_has_not_filled():
    # Plain loop, not @pytest.mark.parametrize -- keeps this file
    # compatible with the project's sandbox-stub pytest runner, which
    # doesn't implement `pytest.mark` (see tests/test_risk.py).
    for status in (OrderStatus.PENDING, OrderStatus.REJECTED, OrderStatus.CANCELED):
        simulated = make_simulated_fill()
        real = make_real_order(status=status, filled_quantity=0.0, filled_avg_price=None)

        with pytest.raises(ValueError):
            reconcile_fill(real, simulated)


def test_raises_when_filled_avg_price_missing_despite_filled_status():
    simulated = make_simulated_fill()
    real = make_real_order(status=OrderStatus.FILLED, filled_avg_price=None)

    with pytest.raises(ValueError):
        reconcile_fill(real, simulated)
