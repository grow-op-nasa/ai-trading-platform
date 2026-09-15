"""Tests for the platform's second permanent strategy
(src/strategies/rsi_mean_reversion.py -- ROADMAP.md, Sprint 6 close-out).

Mirrors `tests/test_ema_cross_strategy.py`'s structure and rigor --
deliberately, since the whole point of this strategy existing is to
prove the same testing approach generalizes to a second, differently
shaped trading idea (mean reversion, not trend following).
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtesting.engine import Backtester
from src.indicators.engine import IndicatorEngine
from src.signals.models import SignalDirection
from src.strategies.rsi_mean_reversion import RSIMeanReversionStrategy


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


# Sharp decline (RSI collapses to 0) followed by a sharp recovery (RSI
# climbs past 70) -- one full oversold-entry / overbought-exit cycle,
# verified empirically against the actual RSI formula, not hand-derived.
CYCLE_CLOSES = [100 - i * 2 for i in range(15)] + [70 + i * 3 for i in range(15)]


def test_rejects_oversold_not_less_than_overbought():
    with pytest.raises(ValueError):
        RSIMeanReversionStrategy(symbol="SPY", oversold=70, overbought=70)
    with pytest.raises(ValueError):
        RSIMeanReversionStrategy(symbol="SPY", oversold=80, overbought=30)


def test_rejects_thresholds_outside_valid_range():
    with pytest.raises(ValueError):
        RSIMeanReversionStrategy(symbol="SPY", oversold=0, overbought=70)
    with pytest.raises(ValueError):
        RSIMeanReversionStrategy(symbol="SPY", oversold=30, overbought=100)


def test_name_is_rsi_mean_reversion():
    assert RSIMeanReversionStrategy(symbol="SPY").name == "rsi_mean_reversion"


def test_prepare_adds_rsi_column_matching_indicator_engine():
    candles = make_candles(CYCLE_CLOSES)
    strategy = RSIMeanReversionStrategy(symbol="SPY", period=5)

    prepared = strategy.prepare(candles)

    expected = IndicatorEngine(candles).calculate("RSI", period=5)
    pd.testing.assert_series_equal(prepared["rsi"], expected, check_names=False)


def test_prepare_does_not_mutate_input():
    candles = make_candles(CYCLE_CLOSES)
    original_columns = list(candles.columns)
    RSIMeanReversionStrategy(symbol="SPY", period=5).prepare(candles)
    assert list(candles.columns) == original_columns


def test_no_signals_when_rsi_never_reaches_oversold():
    # A gently rising series never dips into oversold territory at all.
    candles = make_candles([100 + i * 0.1 for i in range(30)])
    strategy = RSIMeanReversionStrategy(symbol="SPY", period=5)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    assert signals == []


def test_emits_long_then_flat_on_a_single_oversold_overbought_cycle():
    candles = make_candles(CYCLE_CLOSES)
    strategy = RSIMeanReversionStrategy(symbol="SPY", period=5)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    assert len(signals) == 2
    assert signals[0].direction == SignalDirection.LONG
    assert signals[1].direction == SignalDirection.FLAT
    # Sparsity: no two consecutive signals share a direction.
    for a, b in zip(signals, signals[1:]):
        assert a.direction != b.direction


def test_never_emits_short():
    candles = make_candles(CYCLE_CLOSES)
    strategy = RSIMeanReversionStrategy(symbol="SPY", period=5)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    assert all(s.direction != SignalDirection.SHORT for s in signals)


def test_does_not_re_enter_while_already_long():
    # Even if RSI dips back toward oversold again before recovering to
    # overbought, the strategy must not emit a second LONG while one
    # position is already open.
    candles = make_candles([100 - i * 2 for i in range(15)] + [85] * 10 + [70 + i * 3 for i in range(15)])
    strategy = RSIMeanReversionStrategy(symbol="SPY", period=5)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    long_signals = [s for s in signals if s.direction == SignalDirection.LONG]
    assert len(long_signals) == 1


def test_warmup_period_produces_no_signals():
    # The RSI formula's first row is always NaN (diff() has nothing to
    # diff against) -- the isna() guard must skip it, not crash on it.
    candles = make_candles([100, 98, 96, 94])
    strategy = RSIMeanReversionStrategy(symbol="SPY", period=14)
    prepared = strategy.prepare(candles)

    assert pd.isna(prepared["rsi"].iloc[0])
    signals = strategy.generate_signals(prepared)  # must not raise
    assert prepared.index[0] not in {s.timestamp for s in signals}


def test_confidence_is_fixed_and_configurable():
    candles = make_candles(CYCLE_CLOSES)
    strategy = RSIMeanReversionStrategy(symbol="SPY", period=5, confidence=0.33)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    assert signals  # sanity check
    assert all(s.confidence == 0.33 for s in signals)


def test_signal_metadata_includes_strategy_and_reason():
    candles = make_candles(CYCLE_CLOSES)
    strategy = RSIMeanReversionStrategy(symbol="SPY", period=5)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    assert signals
    for signal in signals:
        assert signal.symbol == "SPY"
        assert signal.metadata["strategy"] == "rsi_mean_reversion"
        assert "RSI" in signal.metadata["reason"]


def test_params_exposes_constructor_arguments():
    strategy = RSIMeanReversionStrategy(
        symbol="SPY", period=10, oversold=25.0, overbought=75.0, confidence=0.5
    )
    assert strategy.params == {
        "period": 10,
        "oversold": 25.0,
        "overbought": 75.0,
        "confidence": 0.5,
    }


def test_runs_end_to_end_through_backtester_with_default_periods():
    candles = make_candles(CYCLE_CLOSES)
    strategy = RSIMeanReversionStrategy(symbol="SPY")

    result = Backtester().run(strategy, candles)

    assert result.strategy_name == "rsi_mean_reversion"
    assert isinstance(result.trades, list)
    assert isinstance(result.signals, list)
