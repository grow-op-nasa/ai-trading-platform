"""Tests for the Backtesting Framework (src/backtesting).

Uses small, hand-verifiable price/signal scripts via `ScriptedStrategy`
(a test double that returns a pre-specified signal series) so trade
extraction and the equity curve can be checked exactly, plus one
end-to-end test with a real SMA-crossover strategy that asks the
Indicator Engine for its indicator values -- the pattern every real
strategy is expected to follow.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtesting.engine import Backtester
from src.indicators.engine import IndicatorEngine
from src.strategies.base import SIGNAL_COLUMN


def make_candles(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D", name="timestamp")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        index=dates,
    )


class ScriptedStrategy:
    """Test double: returns a pre-specified signal series regardless of data."""

    def __init__(self, signals: list[int], name: str = "scripted"):
        self._signals = signals
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        return data.copy()

    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({SIGNAL_COLUMN: self._signals}, index=data.index)


class BrokenStrategy:
    """Test double: violates the contract (no 'signal' column)."""

    name = "broken"

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        return data.copy()

    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({"not_signal": [0] * len(data)}, index=data.index)


class SmaCrossStrategy:
    """A minimal real strategy: long when fast SMA > slow SMA, else flat.

    Demonstrates the intended pattern: prepare() asks IndicatorEngine
    for indicator values instead of computing SMA itself.
    """

    name = "sma_cross"

    def __init__(self, fast: int = 3, slow: int = 5):
        self._fast = fast
        self._slow = slow

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        engine = IndicatorEngine(data)
        out = data.copy()
        out["sma_fast"] = engine.calculate("SMA", period=self._fast)
        out["sma_slow"] = engine.calculate("SMA", period=self._slow)
        return out

    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        signal = (data["sma_fast"] > data["sma_slow"]).astype(int)
        return pd.DataFrame({SIGNAL_COLUMN: signal}, index=data.index)


def test_backtester_raises_when_signal_column_missing():
    candles = make_candles([100, 101, 102, 103, 104])

    with pytest.raises(ValueError):
        Backtester().run(BrokenStrategy(), candles)


def test_always_long_produces_one_open_trade_closed_at_end():
    closes = [100, 101, 102, 103, 104]
    candles = make_candles(closes)
    strategy = ScriptedStrategy(signals=[1, 1, 1, 1, 1], name="always_long")

    result = Backtester().run(strategy, candles)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.direction == 1
    assert trade.entry_time == candles.index[0]
    assert trade.exit_time == candles.index[-1]
    assert trade.entry_price == 100
    assert trade.exit_price == 104


def test_always_flat_produces_no_trades():
    closes = [100, 101, 102, 103, 104]
    candles = make_candles(closes)
    strategy = ScriptedStrategy(signals=[0, 0, 0, 0, 0], name="always_flat")

    result = Backtester().run(strategy, candles)

    assert result.trades == []
    assert result.metrics["total_trades"] == 0
    assert result.metrics["win_rate"] is None
    assert result.metrics["sharpe"] is None
    assert result.metrics["total_return_pct"] == 0.0


def test_direction_flip_closes_and_opens_new_trade():
    closes = [100, 101, 99, 98, 97]
    candles = make_candles(closes)
    strategy = ScriptedStrategy(signals=[1, 1, -1, -1, -1], name="flip")

    result = Backtester().run(strategy, candles)

    assert len(result.trades) == 2
    first, second = result.trades
    assert first.direction == 1
    assert first.entry_price == 100
    assert first.exit_price == 99  # closed the instant the flip happens
    assert second.direction == -1
    assert second.entry_price == 99
    assert second.exit_price == 97


def test_equity_curve_has_no_lookahead():
    # A signal set on bar 0 shouldn't affect bar 0's own return -- it
    # can only act on the move from bar 0 to bar 1.
    closes = [100, 200, 200, 200, 200]  # +100% from bar 0 to bar 1
    candles = make_candles(closes)
    strategy = ScriptedStrategy(signals=[1, 0, 0, 0, 0], name="one_shot")

    result = Backtester().run(strategy, candles)

    equity = result.equity_curve
    assert equity.iloc[0] == pytest.approx(100_000.0)
    assert equity.iloc[1] == pytest.approx(200_000.0)
    assert equity.iloc[2] == pytest.approx(200_000.0)


def test_metrics_dict_has_expected_keys():
    closes = [100, 102, 101, 105, 103, 108, 107, 110]
    candles = make_candles(closes)
    strategy = ScriptedStrategy(signals=[1, 1, -1, -1, 1, 1, 0, 0], name="mixed")

    result = Backtester().run(strategy, candles)

    for key in ["total_trades", "win_rate", "sharpe", "total_return_pct", "max_drawdown_pct"]:
        assert key in result.metrics


def test_report_is_a_readable_string():
    closes = [100, 101, 102]
    candles = make_candles(closes)
    strategy = ScriptedStrategy(signals=[1, 1, 1], name="always_long")

    result = Backtester().run(strategy, candles)
    report = result.report()

    assert "always_long" in report
    assert isinstance(report, str)


def test_real_strategy_using_indicator_engine_runs_end_to_end():
    closes = [100, 101, 99, 102, 104, 103, 106, 108, 107, 110]
    candles = make_candles(closes)
    strategy = SmaCrossStrategy(fast=2, slow=4)

    result = Backtester().run(strategy, candles)

    assert result.strategy_name == "sma_cross"
    assert isinstance(result.metrics, dict)
    assert isinstance(result.trades, list)
