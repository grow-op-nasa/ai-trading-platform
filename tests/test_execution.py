"""Tests for paper execution (src/execution).

`PaperBroker` is tested against `Signal`/`SizingDecision` objects built
directly -- no strategy, backtest, or `PositionSizer` call involved,
since this module is deliberately standalone from those this round
(`DECISIONS.md`, ADR-0022), the same posture `src/risk` took toward
`Backtester` (ADR-0021).
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.execution.engine import PaperBroker
from src.execution.models import Fill, Order, OrderSide, Position
from src.risk.models import SizingDecision
from src.signals.models import Signal, SignalDirection


def make_signal(direction: SignalDirection = SignalDirection.LONG) -> Signal:
    return Signal(timestamp=pd.Timestamp("2024-01-01"), direction=direction, confidence=0.8)


def make_sizing_decision(position_size: float = 10.0, approved: bool = True) -> SizingDecision:
    return SizingDecision(
        approved=approved,
        position_size=position_size if approved else 0.0,
        capital_allocated=position_size * 100.0 if approved else 0.0,
        reason="test",
    )


# ---------------------------------------------------------------------------
# PaperBroker construction
# ---------------------------------------------------------------------------


def test_rejects_non_positive_starting_cash():
    with pytest.raises(ValueError):
        PaperBroker(starting_cash=0)
    with pytest.raises(ValueError):
        PaperBroker(starting_cash=-100)


def test_starts_with_no_positions():
    broker = PaperBroker(starting_cash=100_000)
    assert broker.positions == {}
    assert broker.cash == 100_000


# ---------------------------------------------------------------------------
# Opening positions
# ---------------------------------------------------------------------------


def test_opening_long_deducts_cash_and_records_positive_quantity():
    broker = PaperBroker(starting_cash=100_000)
    decision = make_sizing_decision(position_size=10.0)

    fill = broker.submit_signal(
        make_signal(SignalDirection.LONG), "SPY", fill_price=100.0, sizing_decision=decision
    )

    assert broker.cash == pytest.approx(99_000.0)  # paid 10 * 100
    assert fill.cash_delta == pytest.approx(-1_000.0)
    position = broker.positions["SPY"]
    assert position.quantity == pytest.approx(10.0)
    assert position.entry_price == 100.0


def test_opening_short_credits_cash_and_records_negative_quantity():
    broker = PaperBroker(starting_cash=100_000)
    decision = make_sizing_decision(position_size=10.0)

    fill = broker.submit_signal(
        make_signal(SignalDirection.SHORT), "SPY", fill_price=100.0, sizing_decision=decision
    )

    assert broker.cash == pytest.approx(101_000.0)  # received 10 * 100
    assert fill.cash_delta == pytest.approx(1_000.0)
    position = broker.positions["SPY"]
    assert position.quantity == pytest.approx(-10.0)


def test_opening_requires_sizing_decision():
    broker = PaperBroker(starting_cash=100_000)
    with pytest.raises(ValueError):
        broker.submit_signal(make_signal(SignalDirection.LONG), "SPY", fill_price=100.0)


def test_opening_rejects_unapproved_sizing_decision():
    broker = PaperBroker(starting_cash=100_000)
    decision = make_sizing_decision(approved=False)
    with pytest.raises(ValueError):
        broker.submit_signal(
            make_signal(SignalDirection.LONG), "SPY", fill_price=100.0, sizing_decision=decision
        )


def test_opening_when_already_has_a_position_raises():
    broker = PaperBroker(starting_cash=100_000)
    decision = make_sizing_decision(position_size=10.0)
    broker.submit_signal(
        make_signal(SignalDirection.LONG), "SPY", fill_price=100.0, sizing_decision=decision
    )

    with pytest.raises(ValueError):
        broker.submit_signal(
            make_signal(SignalDirection.LONG), "SPY", fill_price=105.0, sizing_decision=decision
        )


# ---------------------------------------------------------------------------
# Closing positions
# ---------------------------------------------------------------------------


def test_closing_long_realizes_profit_into_cash():
    broker = PaperBroker(starting_cash=100_000)
    decision = make_sizing_decision(position_size=10.0)
    broker.submit_signal(
        make_signal(SignalDirection.LONG), "SPY", fill_price=100.0, sizing_decision=decision
    )

    fill = broker.submit_signal(make_signal(SignalDirection.FLAT), "SPY", fill_price=110.0)

    assert fill.order.side == OrderSide.SELL
    assert fill.order.quantity == pytest.approx(10.0)
    # Paid 1000 to open, received 1100 to close -> net +100 profit.
    assert broker.cash == pytest.approx(100_100.0)
    assert "SPY" not in broker.positions


def test_closing_short_realizes_profit_into_cash():
    broker = PaperBroker(starting_cash=100_000)
    decision = make_sizing_decision(position_size=10.0)
    broker.submit_signal(
        make_signal(SignalDirection.SHORT), "SPY", fill_price=100.0, sizing_decision=decision
    )

    fill = broker.submit_signal(make_signal(SignalDirection.FLAT), "SPY", fill_price=90.0)

    assert fill.order.side == OrderSide.BUY
    # Received 1000 to open short, paid 900 to cover -> net +100 profit.
    assert broker.cash == pytest.approx(100_100.0)
    assert "SPY" not in broker.positions


def test_closing_with_no_open_position_raises():
    broker = PaperBroker(starting_cash=100_000)
    with pytest.raises(ValueError):
        broker.submit_signal(make_signal(SignalDirection.FLAT), "SPY", fill_price=100.0)


def test_closing_a_losing_long_reduces_cash_below_starting():
    broker = PaperBroker(starting_cash=100_000)
    decision = make_sizing_decision(position_size=10.0)
    broker.submit_signal(
        make_signal(SignalDirection.LONG), "SPY", fill_price=100.0, sizing_decision=decision
    )

    broker.submit_signal(make_signal(SignalDirection.FLAT), "SPY", fill_price=90.0)

    assert broker.cash == pytest.approx(99_900.0)  # lost 100


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_submit_signal_rejects_non_positive_fill_price():
    broker = PaperBroker(starting_cash=100_000)
    decision = make_sizing_decision(position_size=10.0)
    with pytest.raises(ValueError):
        broker.submit_signal(
            make_signal(SignalDirection.LONG), "SPY", fill_price=0.0, sizing_decision=decision
        )
    with pytest.raises(ValueError):
        broker.submit_signal(
            make_signal(SignalDirection.LONG), "SPY", fill_price=-10.0, sizing_decision=decision
        )


def test_order_rejects_non_positive_quantity():
    with pytest.raises(ValueError):
        Order(
            symbol="SPY",
            side=OrderSide.BUY,
            quantity=0,
            signal_id=make_signal().id,
            timestamp=pd.Timestamp("2024-01-01"),
        )


# ---------------------------------------------------------------------------
# account_state
# ---------------------------------------------------------------------------


def test_account_state_equity_unchanged_immediately_after_opening_long():
    broker = PaperBroker(starting_cash=100_000)
    decision = make_sizing_decision(position_size=10.0)
    broker.submit_signal(
        make_signal(SignalDirection.LONG), "SPY", fill_price=100.0, sizing_decision=decision
    )

    account = broker.account_state
    assert account.equity == pytest.approx(100_000.0)
    assert account.open_exposure == pytest.approx(1_000.0)


def test_account_state_equity_unchanged_immediately_after_opening_short():
    broker = PaperBroker(starting_cash=100_000)
    decision = make_sizing_decision(position_size=10.0)
    broker.submit_signal(
        make_signal(SignalDirection.SHORT), "SPY", fill_price=100.0, sizing_decision=decision
    )

    account = broker.account_state
    assert account.equity == pytest.approx(100_000.0)
    assert account.open_exposure == pytest.approx(1_000.0)


def test_account_state_reflects_realized_pnl_after_closing():
    broker = PaperBroker(starting_cash=100_000)
    decision = make_sizing_decision(position_size=10.0)
    broker.submit_signal(
        make_signal(SignalDirection.LONG), "SPY", fill_price=100.0, sizing_decision=decision
    )
    broker.submit_signal(make_signal(SignalDirection.FLAT), "SPY", fill_price=110.0)

    account = broker.account_state
    assert account.equity == pytest.approx(100_100.0)
    assert account.open_exposure == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Traceability and defensive copies
# ---------------------------------------------------------------------------


def test_order_traces_back_to_originating_signal():
    signal = make_signal(SignalDirection.LONG)
    broker = PaperBroker(starting_cash=100_000)
    decision = make_sizing_decision(position_size=10.0)

    fill = broker.submit_signal(signal, "SPY", fill_price=100.0, sizing_decision=decision)

    assert fill.order.signal_id == signal.id
    assert fill.order.timestamp == signal.timestamp
    assert broker.positions["SPY"].entry_signal_id == signal.id


def test_positions_property_returns_defensive_copy():
    broker = PaperBroker(starting_cash=100_000)
    decision = make_sizing_decision(position_size=10.0)
    broker.submit_signal(
        make_signal(SignalDirection.LONG), "SPY", fill_price=100.0, sizing_decision=decision
    )

    positions = broker.positions
    del positions["SPY"]

    assert "SPY" in broker.positions  # internal state untouched
