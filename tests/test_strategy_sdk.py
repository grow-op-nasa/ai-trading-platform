"""Tests for the Strategy SDK (src/strategies/sdk.py).

Covers `BaseStrategy`'s helpers in isolation, then one end-to-end demo
(`EMACrossStrategy`, the platform's real permanent strategy -- see
`src/strategies/ema_cross.py`) run through the real `Backtester` -- the
same "real strategy calling IndicatorEngine" pattern `SmaCrossStrategy`
demonstrated in `tests/test_backtesting.py`, but built on the SDK.
`EMACrossStrategy`'s own trading logic (crossover detection, warmup
handling, long-only behavior) is tested in `tests/test_ema_cross_strategy.py`,
not duplicated here -- these tests only care that a real SDK-based
strategy integrates correctly with `Backtester`.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtesting.engine import Backtester
from src.indicators.engine import IndicatorEngine
from src.signals.models import SignalDirection
from src.strategies.ema_cross import EMACrossStrategy
from src.strategies.sdk import BaseStrategy


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


class MinimalStrategy(BaseStrategy):
    """The smallest possible concrete subclass, for testing helpers."""

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        return data.copy()

    def generate_signals(self, data: pd.DataFrame) -> list:
        return []


def test_base_strategy_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        BaseStrategy(name="not allowed")  # type: ignore[abstract]


def test_name_property_returns_constructor_value():
    strategy = MinimalStrategy(name="minimal", symbol="SPY")
    assert strategy.name == "minimal"


def test_symbol_property_returns_constructor_value():
    strategy = MinimalStrategy(name="minimal", symbol="SPY")
    assert strategy.symbol == "SPY"


def test_indicator_matches_direct_indicator_engine_call():
    candles = make_candles([100, 101, 102, 103, 104, 105])
    strategy = MinimalStrategy(name="minimal", symbol="SPY")

    via_sdk = strategy.indicator(candles, "SMA", period=3)
    via_engine = IndicatorEngine(candles).calculate("SMA", period=3)

    pd.testing.assert_series_equal(via_sdk, via_engine)


def test_require_columns_passes_when_present():
    candles = make_candles([100, 101, 102])
    strategy = MinimalStrategy(name="minimal", symbol="SPY")
    strategy.require_columns(candles, "close", "volume")  # should not raise


def test_require_columns_defaults_to_ohlcv():
    candles = make_candles([100, 101, 102])
    strategy = MinimalStrategy(name="minimal", symbol="SPY")
    strategy.require_columns(candles)  # no columns given -> defaults to OHLCV


def test_require_columns_raises_with_clear_message_when_missing():
    candles = make_candles([100, 101, 102]).drop(columns=["volume"])
    strategy = MinimalStrategy(name="minimal", symbol="SPY")

    with pytest.raises(ValueError) as exc_info:
        strategy.require_columns(candles)

    assert "minimal" in str(exc_info.value)
    assert "volume" in str(exc_info.value)


def test_emit_signal_merges_strategy_name_into_metadata():
    strategy = MinimalStrategy(name="minimal", symbol="SPY")
    signal = strategy.emit_signal(
        pd.Timestamp("2024-01-01"), SignalDirection.LONG, confidence=0.9, reason="test"
    )

    assert signal.metadata["strategy"] == "minimal"
    assert signal.metadata["reason"] == "test"


def test_emit_signal_attaches_the_strategys_symbol():
    strategy = MinimalStrategy(name="minimal", symbol="SPY")
    signal = strategy.emit_signal(
        pd.Timestamp("2024-01-01"), SignalDirection.LONG, confidence=0.9
    )

    assert signal.symbol == "SPY"


def test_emit_signal_author_supplied_strategy_key_wins_on_collision():
    strategy = MinimalStrategy(name="minimal", symbol="SPY")
    signal = strategy.emit_signal(
        pd.Timestamp("2024-01-01"),
        SignalDirection.LONG,
        confidence=0.9,
        strategy="overridden",
    )

    assert signal.metadata["strategy"] == "overridden"


def test_emit_signal_rejects_invalid_confidence():
    strategy = MinimalStrategy(name="minimal", symbol="SPY")
    with pytest.raises(ValueError):
        strategy.emit_signal(pd.Timestamp("2024-01-01"), SignalDirection.LONG, confidence=1.5)


def test_ema_cross_strategy_runs_end_to_end_through_backtester():
    closes = [100, 101, 99, 102, 104, 103, 106, 108, 107, 110, 112, 111]
    candles = make_candles(closes)
    strategy = EMACrossStrategy(symbol="SPY", fast=2, slow=4)

    result = Backtester().run(strategy, candles)

    assert result.strategy_name == "ema_cross"
    assert isinstance(result.trades, list)
    assert isinstance(result.signals, list)
    for signal in result.signals:
        assert signal.symbol == "SPY"
        assert signal.metadata["strategy"] == "ema_cross"
        assert "reason" in signal.metadata


def test_ema_cross_strategy_instance_is_reusable_across_runs():
    # self.indicator() is stateless, so the same instance should work
    # against a second, different candle set without stale state.
    strategy = EMACrossStrategy(symbol="SPY", fast=2, slow=4)
    first_candles = make_candles([100, 101, 99, 102, 104, 103, 106, 108])
    second_candles = make_candles([50, 49, 51, 53, 52, 55, 54, 58])

    first_result = Backtester().run(strategy, first_candles)
    second_result = Backtester().run(strategy, second_candles)

    assert first_result.strategy_name == second_result.strategy_name == "ema_cross"
