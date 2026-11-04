"""Equity-timing regression tests -- Sprint 23 closeout (ADR-0061 amendment, research engine v3).

Convention under test: the equity curve at bar t reflects only fills that happened at or before bar t.
Under NEXT_BAR_OPEN a signal at bar t fills at bar t+1's open, so:
  * the signal bar's equity must NOT contain the entry (nor book `close_t -> open_t+1` as P&L),
  * the fill bar's equity is cash after the fill plus the position marked at that bar's close,
  * an exit signalled at bar t leaves the position (marked at close_t) in equity until the exit fill bar.
Every expectation below is hand-calculable from the candles and the trade's own quantity/fees.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtesting.config import BacktestConfig
from src.backtesting.engine import Backtester
from src.backtesting.execution_model import (
    ExecutionConfig,
    ExecutionTiming,
    PercentageFeeModel,
)
from src.backtesting.stop_policy import ATRStopPolicy
from src.risk.models import PortfolioRiskLimits, RiskLimits
from src.signals.models import Signal, SignalDirection

CASH = 100_000.0
N = 14
ENTRY_BAR, ENTRY_FILL_OPEN = 6, 102.0     # signal at bar 6 -> fill at bar 7's open
EXIT_BAR, EXIT_FILL_OPEN = 9, 104.0       # signal at bar 9 -> fill at bar 10's open


class Scripted:
    name = "scripted"

    def __init__(self, signals):
        self._signals = signals

    def prepare(self, data):
        return data.copy()

    def generate_signals(self, data):
        return list(self._signals)


def candles() -> pd.DataFrame:
    close = [100.0] * N
    open_ = [100.0] * N
    open_[ENTRY_BAR + 1] = ENTRY_FILL_OPEN
    for i in (ENTRY_BAR + 1, ENTRY_BAR + 2, ENTRY_BAR + 3):      # bars 7, 8, 9 close at 103
        close[i] = 103.0
    open_[EXIT_BAR + 1] = EXIT_FILL_OPEN
    close[EXIT_BAR + 1] = 105.0
    idx = pd.date_range("2024-01-01", periods=N, freq="D", name="timestamp")
    return pd.DataFrame(
        {
            "open": open_,
            "high": [max(o, c) + 0.5 for o, c in zip(open_, close)],
            "low": [min(o, c) - 0.5 for o, c in zip(open_, close)],
            "close": close,
            "volume": [1000.0] * N,
        },
        index=idx,
    )


def config(timing=ExecutionTiming.NEXT_BAR_OPEN, fee_bps=0.0) -> BacktestConfig:
    return BacktestConfig(
        initial_cash=CASH,
        stop_policy=ATRStopPolicy(period=3, multiple=1.0),
        risk_limits=RiskLimits(allocation_per_trade_pct=0.3, max_portfolio_exposure_pct=1.0),
        portfolio_risk_limits=PortfolioRiskLimits(risk_pct_per_trade=0.05),
        execution_config=ExecutionConfig(timing=timing, fee_model=PercentageFeeModel(fee_bps=fee_bps)),
    )


def run(signals, cfg):
    data = candles()
    result = Backtester().run_portfolio({"AAA": Scripted(signals)}, {"AAA": data}, cfg)
    return data, result


def sig(data, bar, direction):
    return Signal(timestamp=data.index[bar], symbol="AAA", direction=direction, confidence=0.9)


def both(data):
    return [sig(data, ENTRY_BAR, SignalDirection.LONG), sig(data, EXIT_BAR, SignalDirection.FLAT)]


def test_signal_bar_equity_does_not_contain_a_fill_that_has_not_happened_yet():
    data = candles()
    _, result = run(both(data), config())
    q = result.trades[0].quantity
    eq = result.equity_curve
    assert q > 0
    assert (eq.iloc[: ENTRY_BAR + 1] == CASH).all()                       # nothing before/at the signal bar
    # The old behaviour would have booked q * (100 - 102) at the signal bar.
    assert eq.iloc[ENTRY_BAR] == pytest.approx(CASH)


def test_fill_bar_equity_is_cash_after_fill_plus_position_at_that_bars_close():
    data = candles()
    _, result = run(both(data), config())
    q = result.trades[0].quantity
    eq = result.equity_curve
    assert result.trades[0].entry_fill_price == pytest.approx(ENTRY_FILL_OPEN)
    assert eq.iloc[ENTRY_BAR + 1] == pytest.approx(CASH + q * (103.0 - ENTRY_FILL_OPEN))
    assert eq.iloc[ENTRY_BAR + 2] == pytest.approx(CASH + q * (103.0 - ENTRY_FILL_OPEN))


def test_exit_signal_bar_still_holds_the_position_until_the_exit_fill_bar():
    data = candles()
    _, result = run(both(data), config())
    q = result.trades[0].quantity
    eq = result.equity_curve
    assert result.trades[0].exit_fill_price == pytest.approx(EXIT_FILL_OPEN)
    # Signal bar for the exit: position valued at close (103), proceeds not yet received.
    assert eq.iloc[EXIT_BAR] == pytest.approx(CASH + q * (103.0 - ENTRY_FILL_OPEN))
    # Exit fill bar: all cash, realised at the exit open (not the 105 close).
    assert eq.iloc[EXIT_BAR + 1] == pytest.approx(CASH + q * (EXIT_FILL_OPEN - ENTRY_FILL_OPEN))
    assert eq.iloc[-1] == pytest.approx(result.final_portfolio.cash)


def test_costs_are_charged_on_the_fill_bar_not_the_signal_bar():
    data = candles()
    _, result = run(both(data), config(fee_bps=10.0))
    trade = result.trades[0]
    q, fe, fx = trade.quantity, trade.entry_fee, trade.exit_fee
    eq = result.equity_curve
    assert fe > 0 and fx > 0
    assert eq.iloc[ENTRY_BAR] == pytest.approx(CASH)                      # entry fee not yet paid
    assert eq.iloc[ENTRY_BAR + 1] == pytest.approx(CASH - fe + q * (103.0 - ENTRY_FILL_OPEN))
    assert eq.iloc[EXIT_BAR] == pytest.approx(CASH - fe + q * (103.0 - ENTRY_FILL_OPEN))   # exit fee not yet paid
    assert eq.iloc[EXIT_BAR + 1] == pytest.approx(CASH - fe - fx + q * (EXIT_FILL_OPEN - ENTRY_FILL_OPEN))


def test_cash_plus_positions_reconcile_to_equity_after_every_fill():
    data = candles()
    _, result = run(both(data), config(fee_bps=10.0))
    # No open position at the end: equity equals cash exactly; trade P&L net of both fees explains the change.
    trade = result.trades[0]
    gross = trade.quantity * (trade.exit_fill_price - trade.entry_fill_price)
    assert result.equity_curve.iloc[-1] == pytest.approx(CASH + gross - trade.entry_fee - trade.exit_fee)
    assert result.equity_curve.iloc[-1] == pytest.approx(result.final_portfolio.cash)


def test_a_still_open_position_at_the_end_is_marked_at_the_last_close():
    data = candles()
    _, result = run([sig(data, ENTRY_BAR, SignalDirection.LONG)], config())
    q = result.trades[0].quantity
    assert candles()["close"].iloc[-1] == 100.0
    assert result.equity_curve.iloc[-1] == pytest.approx(CASH + q * (100.0 - ENTRY_FILL_OPEN))
    assert result.final_portfolio.positions                                # genuinely still open


def test_no_trade_run_has_a_flat_equity_curve():
    _, result = run([], config())
    assert (result.equity_curve == CASH).all() and len(result.equity_curve) == N


def test_signal_bar_close_fills_are_unchanged_by_the_correction():
    """Same-bar fills are observed on the bar they happen, so nothing is reversed."""
    data = candles()
    _, result = run(both(data), config(timing=ExecutionTiming.SIGNAL_BAR_CLOSE))
    q = result.trades[0].quantity
    eq = result.equity_curve
    assert result.trades[0].entry_fill_price == pytest.approx(100.0)
    assert eq.iloc[ENTRY_BAR] == pytest.approx(CASH)                       # filled at close 100, marked at close 100
    assert eq.iloc[ENTRY_BAR + 1] == pytest.approx(CASH + q * 3.0)         # close 103
    assert eq.iloc[EXIT_BAR] == pytest.approx(CASH + q * 3.0)              # exit fills at close 103 on the signal bar
    assert eq.iloc[-1] == pytest.approx(CASH + q * 3.0)
