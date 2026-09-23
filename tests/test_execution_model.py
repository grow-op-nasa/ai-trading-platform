"""Unit tests for `src.backtesting.execution_model` -- Sprint 12
(`DECISIONS.md`, ADR-0045).

Covers spec sections 36-39: execution timing (Tests A/B/C), the
look-ahead-protection regression, slippage, and fees -- all at the
`ExecutionModel.simulate()` unit level, independent of the
`PortfolioBacktestEngine` integration (that lives in
`tests/test_portfolio_backtest_engine.py`).
"""

from __future__ import annotations

from uuid import uuid4

import pandas as pd
import pytest

from src.backtesting.execution_model import (
    INSUFFICIENT_CASH_FOR_FEE,
    NO_EXECUTION_BAR,
    ExecutionConfig,
    ExecutionModel,
    ExecutionOutcome,
    ExecutionTiming,
    PercentageFeeModel,
    PercentageSlippageModel,
)
from src.execution.models import Fill, Order, OrderSide


def make_candles(rows: list[dict], freq: str = "D") -> pd.DataFrame:
    """Build a minimal OHLC(V) frame indexed like the rest of the
    Sprint 11/12 test fixtures -- each row is a dict of column overrides
    (missing columns default to a flat 100.0/1000.0)."""
    index = pd.date_range("2024-01-01", periods=len(rows), freq=freq, name="timestamp")
    data = []
    for row in rows:
        data.append(
            {
                "open": row.get("open", 100.0),
                "high": row.get("high", 100.0),
                "low": row.get("low", 100.0),
                "close": row.get("close", 100.0),
                "volume": row.get("volume", 1000.0),
            }
        )
    return pd.DataFrame(data, index=index)


def make_order(timestamp: pd.Timestamp, side: OrderSide = OrderSide.BUY, quantity: float = 10.0) -> Order:
    return Order(symbol="AAPL", side=side, quantity=quantity, signal_id=uuid4(), timestamp=timestamp)


# ---------------------------------------------------------------------------
# ExecutionOutcome validation
# ---------------------------------------------------------------------------


def test_filled_outcome_requires_a_fill():
    with pytest.raises(ValueError):
        ExecutionOutcome(filled=True, fill=None)


def test_unfilled_outcome_requires_a_reason():
    with pytest.raises(ValueError):
        ExecutionOutcome(filled=False, reason=None)


def test_unfilled_outcome_with_reason_is_valid():
    outcome = ExecutionOutcome(filled=False, reason=NO_EXECUTION_BAR)
    assert outcome.fill is None
    assert outcome.reason == NO_EXECUTION_BAR


# ---------------------------------------------------------------------------
# Section 36 -- Test A: SIGNAL_BAR_CLOSE default timing
# ---------------------------------------------------------------------------


def test_signal_bar_close_fills_at_the_signal_bar_own_close():
    candles = make_candles([{"close": 100.0}, {"close": 105.0}, {"close": 110.0}])
    order = make_order(candles.index[0])
    model = ExecutionModel(ExecutionConfig(timing=ExecutionTiming.SIGNAL_BAR_CLOSE))

    outcome = model.simulate(order, candles)

    assert outcome.filled
    assert outcome.fill.reference_price == pytest.approx(100.0)
    assert outcome.fill.fill_timestamp == candles.index[0]


# ---------------------------------------------------------------------------
# Section 36 -- Test A/B: NEXT_BAR_OPEN fills at bar N+1's timestamp/open
# ---------------------------------------------------------------------------


def test_next_bar_open_fill_timestamp_is_bar_n_plus_1():
    candles = make_candles(
        [{"open": 100.0, "close": 100.0}, {"open": 101.0, "close": 108.0}, {"open": 109.0, "close": 111.0}]
    )
    order = make_order(candles.index[0])
    model = ExecutionModel(ExecutionConfig(timing=ExecutionTiming.NEXT_BAR_OPEN))

    outcome = model.simulate(order, candles)

    assert outcome.filled
    # Test A: fill timestamp is bar N+1's timestamp, never bar N's.
    assert outcome.fill.fill_timestamp == candles.index[1]


def test_next_bar_open_fill_price_uses_only_bar_n_plus_1_open():
    # Bar N (index 0): close=100 -- must never be used.
    # Bar N+1 (index 1): open=101, high=999, low=1, close=500 -- only
    # the open (101) may determine the fill price (Test B).
    candles = make_candles(
        [
            {"open": 50.0, "high": 50.0, "low": 50.0, "close": 100.0},
            {"open": 101.0, "high": 999.0, "low": 1.0, "close": 500.0},
        ]
    )
    order = make_order(candles.index[0])
    model = ExecutionModel(ExecutionConfig(timing=ExecutionTiming.NEXT_BAR_OPEN))

    outcome = model.simulate(order, candles)

    assert outcome.filled
    assert outcome.fill.reference_price == pytest.approx(101.0)
    assert outcome.fill.fill_price == pytest.approx(101.0)  # zero slippage default


# ---------------------------------------------------------------------------
# Section 36 -- Test C / Section 7: no next bar -> explicit NO_EXECUTION_BAR
# ---------------------------------------------------------------------------


def test_next_bar_open_at_the_final_bar_is_explicitly_unfilled():
    candles = make_candles([{"close": 100.0}, {"close": 105.0}])
    order = make_order(candles.index[-1])  # the final available bar
    model = ExecutionModel(ExecutionConfig(timing=ExecutionTiming.NEXT_BAR_OPEN))

    outcome = model.simulate(order, candles)

    assert outcome.filled is False
    assert outcome.fill is None
    assert outcome.reason == NO_EXECUTION_BAR


def test_order_timestamp_not_in_candles_is_explicitly_unfilled():
    candles = make_candles([{"close": 100.0}, {"close": 105.0}])
    outside_ts = candles.index[-1] + pd.Timedelta(days=50)
    order = make_order(outside_ts)
    model = ExecutionModel()  # default SIGNAL_BAR_CLOSE

    outcome = model.simulate(order, candles)

    assert outcome.filled is False
    assert outcome.reason == NO_EXECUTION_BAR


# ---------------------------------------------------------------------------
# Section 37 -- look-ahead-protection regression
# ---------------------------------------------------------------------------


def test_favorable_future_high_low_close_never_improves_the_fill():
    # Bar N+1 has an extremely favorable high/low/close, but an open
    # identical to a "boring" control candle -- the fill must be
    # identical either way, proving high/low/close of N+1 are never
    # consulted.
    boring_candles = make_candles(
        [{"open": 100.0, "close": 100.0}, {"open": 101.0, "high": 101.5, "low": 100.5, "close": 101.0}]
    )
    favorable_candles = make_candles(
        [{"open": 100.0, "close": 100.0}, {"open": 101.0, "high": 10_000.0, "low": 0.01, "close": 9_999.0}]
    )
    model = ExecutionModel(ExecutionConfig(timing=ExecutionTiming.NEXT_BAR_OPEN))

    boring_order = make_order(boring_candles.index[0])
    favorable_order = make_order(favorable_candles.index[0])
    boring_outcome = model.simulate(boring_order, boring_candles)
    favorable_outcome = model.simulate(favorable_order, favorable_candles)

    assert boring_outcome.filled and favorable_outcome.filled
    assert boring_outcome.fill.fill_price == pytest.approx(favorable_outcome.fill.fill_price)
    assert favorable_outcome.fill.fill_price == pytest.approx(101.0)


# ---------------------------------------------------------------------------
# Section 38 -- slippage
# ---------------------------------------------------------------------------


def test_zero_slippage_fill_equals_reference_price_exactly():
    candles = make_candles([{"close": 100.0}])
    order = make_order(candles.index[0])
    model = ExecutionModel(ExecutionConfig(slippage_model=PercentageSlippageModel(0.0)))

    outcome = model.simulate(order, candles)

    assert outcome.fill.fill_price == outcome.fill.reference_price == pytest.approx(100.0)
    assert outcome.fill.slippage_amount == pytest.approx(0.0)


def test_positive_slippage_on_a_buy_is_worse_than_reference():
    candles = make_candles([{"close": 100.0}])
    order = make_order(candles.index[0], side=OrderSide.BUY)
    model = ExecutionModel(ExecutionConfig(slippage_model=PercentageSlippageModel(10.0)))

    outcome = model.simulate(order, candles)

    assert outcome.fill.fill_price > outcome.fill.reference_price
    assert outcome.fill.fill_price == pytest.approx(100.0 * 1.001)


def test_positive_slippage_on_a_sell_is_worse_than_reference():
    candles = make_candles([{"close": 100.0}])
    order = make_order(candles.index[0], side=OrderSide.SELL)
    model = ExecutionModel(ExecutionConfig(slippage_model=PercentageSlippageModel(10.0)))

    outcome = model.simulate(order, candles)

    assert outcome.fill.fill_price < outcome.fill.reference_price
    assert outcome.fill.fill_price == pytest.approx(100.0 * 0.999)


def test_slippage_direction_is_never_the_same_formula_for_both_sides():
    # Regression against `price * (1 - slippage)` applied uniformly --
    # buy and sell fills must move in opposite directions relative to
    # the same reference price.
    candles = make_candles([{"close": 100.0}])
    model = ExecutionModel(ExecutionConfig(slippage_model=PercentageSlippageModel(25.0)))

    buy_outcome = model.simulate(make_order(candles.index[0], side=OrderSide.BUY), candles)
    sell_outcome = model.simulate(make_order(candles.index[0], side=OrderSide.SELL), candles)

    assert buy_outcome.fill.fill_price > 100.0
    assert sell_outcome.fill.fill_price < 100.0
    assert buy_outcome.fill.fill_price != sell_outcome.fill.fill_price


def test_slippage_is_deterministic_across_repeated_simulation():
    candles = make_candles([{"close": 100.0}])
    order = make_order(candles.index[0])
    model = ExecutionModel(ExecutionConfig(slippage_model=PercentageSlippageModel(7.5)))

    first = model.simulate(order, candles)
    second = model.simulate(order, candles)

    assert first.fill.fill_price == second.fill.fill_price


def test_negative_slippage_bps_is_rejected():
    with pytest.raises(ValueError):
        PercentageSlippageModel(-1.0)


# ---------------------------------------------------------------------------
# Section 39 -- fees
# ---------------------------------------------------------------------------


def test_zero_fee_charges_nothing():
    candles = make_candles([{"close": 100.0}])
    order = make_order(candles.index[0], quantity=10.0)
    model = ExecutionModel(ExecutionConfig(fee_model=PercentageFeeModel(0.0, 0.0)))

    outcome = model.simulate(order, candles)

    assert outcome.fill.fee == pytest.approx(0.0)


def test_positive_fee_is_a_percentage_of_notional():
    candles = make_candles([{"close": 100.0}])
    order = make_order(candles.index[0], quantity=10.0)  # notional = 1000.0
    model = ExecutionModel(ExecutionConfig(fee_model=PercentageFeeModel(fee_bps=10.0)))  # 0.10%

    outcome = model.simulate(order, candles)

    assert outcome.fill.fee == pytest.approx(1000.0 * 0.0010)


def test_fee_applies_consistently_to_buy_and_sell_by_default():
    candles = make_candles([{"close": 100.0}])
    model = ExecutionModel(ExecutionConfig(fee_model=PercentageFeeModel(fee_bps=20.0)))

    buy_outcome = model.simulate(make_order(candles.index[0], side=OrderSide.BUY, quantity=5.0), candles)
    sell_outcome = model.simulate(make_order(candles.index[0], side=OrderSide.SELL, quantity=5.0), candles)

    assert buy_outcome.fill.fee == pytest.approx(sell_outcome.fill.fee)


def test_fixed_fee_component_is_added_to_the_percentage_component():
    candles = make_candles([{"close": 100.0}])
    order = make_order(candles.index[0], quantity=10.0)  # notional = 1000.0
    model = ExecutionModel(ExecutionConfig(fee_model=PercentageFeeModel(fee_bps=10.0, fixed_fee=1.5)))

    outcome = model.simulate(order, candles)

    assert outcome.fill.fee == pytest.approx(1000.0 * 0.0010 + 1.5)


def test_negative_fee_bps_or_fixed_fee_is_rejected():
    with pytest.raises(ValueError):
        PercentageFeeModel(fee_bps=-1.0)
    with pytest.raises(ValueError):
        PercentageFeeModel(fee_bps=0.0, fixed_fee=-1.0)


# ---------------------------------------------------------------------------
# Cash-delta / cost-accounting correctness at the unit level
# ---------------------------------------------------------------------------


def test_buy_cash_delta_includes_both_notional_and_fee():
    candles = make_candles([{"close": 100.0}])
    order = make_order(candles.index[0], side=OrderSide.BUY, quantity=10.0)
    model = ExecutionModel(
        ExecutionConfig(
            slippage_model=PercentageSlippageModel(0.0),
            fee_model=PercentageFeeModel(fee_bps=10.0),
        )
    )

    outcome = model.simulate(order, candles)
    fill = outcome.fill

    expected_notional = 10.0 * fill.fill_price
    expected_fee = expected_notional * 0.0010
    assert fill.fee == pytest.approx(expected_fee)
    assert fill.cash_delta == pytest.approx(-expected_notional - expected_fee)


def test_sell_cash_delta_includes_notional_minus_fee():
    candles = make_candles([{"close": 100.0}])
    order = make_order(candles.index[0], side=OrderSide.SELL, quantity=10.0)
    model = ExecutionModel(
        ExecutionConfig(
            slippage_model=PercentageSlippageModel(0.0),
            fee_model=PercentageFeeModel(fee_bps=10.0),
        )
    )

    outcome = model.simulate(order, candles)
    fill = outcome.fill

    expected_notional = 10.0 * fill.fill_price
    expected_fee = expected_notional * 0.0010
    assert fill.cash_delta == pytest.approx(expected_notional - expected_fee)


# ---------------------------------------------------------------------------
# ExecutionConfig identity / describe()
# ---------------------------------------------------------------------------


def test_execution_config_describe_reflects_timing_slippage_and_fees():
    config = ExecutionConfig(
        timing=ExecutionTiming.NEXT_BAR_OPEN,
        slippage_model=PercentageSlippageModel(5.0),
        fee_model=PercentageFeeModel(fee_bps=1.0, fixed_fee=0.0),
    )

    described = config.describe()

    assert described["timing"] == "NEXT_BAR_OPEN"
    assert described["slippage"]["slippage_bps"] == 5.0
    assert described["fee"]["fee_bps"] == 1.0


def test_default_execution_config_is_zero_cost_signal_bar_close():
    config = ExecutionConfig()

    described = config.describe()

    assert described["timing"] == "SIGNAL_BAR_CLOSE"
    assert described["slippage"]["slippage_bps"] == 0.0
    assert described["fee"]["fee_bps"] == 0.0
    assert described["fee"]["fixed_fee"] == 0.0


def test_two_configs_differing_only_in_slippage_describe_differently():
    zero = ExecutionConfig(slippage_model=PercentageSlippageModel(0.0))
    nonzero = ExecutionConfig(slippage_model=PercentageSlippageModel(10.0))

    assert zero.describe() != nonzero.describe()


def test_two_configs_differing_only_in_timing_describe_differently():
    close_mode = ExecutionConfig(timing=ExecutionTiming.SIGNAL_BAR_CLOSE)
    next_bar_mode = ExecutionConfig(timing=ExecutionTiming.NEXT_BAR_OPEN)

    assert close_mode.describe() != next_bar_mode.describe()


# ---------------------------------------------------------------------------
# Purity -- same inputs, same output, no hidden state.
# ---------------------------------------------------------------------------


def test_simulate_is_pure_same_inputs_same_output():
    candles = make_candles([{"open": 100.0, "close": 100.0}, {"open": 102.0, "close": 103.0}])
    order = make_order(candles.index[0])
    model = ExecutionModel(
        ExecutionConfig(
            timing=ExecutionTiming.NEXT_BAR_OPEN,
            slippage_model=PercentageSlippageModel(5.0),
            fee_model=PercentageFeeModel(fee_bps=2.0),
        )
    )

    first = model.simulate(order, candles)
    second = model.simulate(order, candles)

    assert first.fill.fill_price == second.fill.fill_price
    assert first.fill.fee == second.fill.fee
    assert first.fill.fill_timestamp == second.fill.fill_timestamp
