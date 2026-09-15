"""Tests for the platform's first permanent strategy
(src/strategies/ema_cross.py).

`tests/test_strategy_sdk.py` already covers `EMACrossStrategy`'s
integration with the SDK and `Backtester` end to end -- these tests
focus on the strategy's own trading logic in isolation: validation,
crossover detection, sparsity, warmup handling, and long-only behavior.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtesting.engine import Backtester
from src.indicators.engine import IndicatorEngine
from src.signals.models import SignalDirection
from src.strategies.ema_cross import EMACrossStrategy


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


def test_rejects_fast_period_not_less_than_slow():
    with pytest.raises(ValueError):
        EMACrossStrategy(symbol="SPY", fast=10, slow=10)
    with pytest.raises(ValueError):
        EMACrossStrategy(symbol="SPY", fast=20, slow=10)


def test_name_is_ema_cross():
    assert EMACrossStrategy(symbol="SPY", fast=2, slow=4).name == "ema_cross"


def test_prepare_adds_ema_columns_matching_indicator_engine():
    candles = make_candles([100, 101, 102, 103, 104, 105, 106, 108])
    strategy = EMACrossStrategy(symbol="SPY", fast=2, slow=4)

    prepared = strategy.prepare(candles)

    expected_fast = IndicatorEngine(candles).calculate("EMA", period=2)
    expected_slow = IndicatorEngine(candles).calculate("EMA", period=4)
    pd.testing.assert_series_equal(prepared["ema_fast"], expected_fast, check_names=False)
    pd.testing.assert_series_equal(prepared["ema_slow"], expected_slow, check_names=False)


def test_prepare_does_not_mutate_input():
    candles = make_candles([100, 101, 102, 103, 104])
    original_columns = list(candles.columns)
    EMACrossStrategy(symbol="SPY", fast=2, slow=4).prepare(candles)
    assert list(candles.columns) == original_columns


def test_no_signals_when_never_crosses():
    # A strictly rising series with a fast EMA already above the slow
    # EMA before any non-NaN row -- no crossover event ever occurs.
    candles = make_candles([100 + i for i in range(20)])
    strategy = EMACrossStrategy(symbol="SPY", fast=2, slow=4)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    # Either zero signals (fast already above slow at the first valid
    # row) or exactly one LONG entry and nothing else -- never a FLAT
    # without a preceding LONG, and never more than one direction change
    # for monotonically diverging EMAs.
    directions = [s.direction for s in signals]
    assert directions in ([], [SignalDirection.LONG])


def test_emits_long_then_flat_on_a_single_crossover_cycle():
    # Falls, giving fast < slow, then rises sharply, giving fast > slow,
    # then falls again -- one full up-cross/down-cross cycle.
    closes = [110, 108, 106, 104, 102, 100, 105, 110, 115, 120, 110, 100, 90, 80]
    candles = make_candles(closes)
    strategy = EMACrossStrategy(symbol="SPY", fast=2, slow=4)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    assert len(signals) >= 2
    assert signals[0].direction == SignalDirection.LONG
    assert signals[1].direction == SignalDirection.FLAT
    # Sparsity: no two consecutive signals share a direction.
    for a, b in zip(signals, signals[1:]):
        assert a.direction != b.direction


def test_never_emits_short():
    closes = [120, 110, 100, 90, 80, 90, 100, 110, 120, 130, 120, 110, 100, 90]
    candles = make_candles(closes)
    strategy = EMACrossStrategy(symbol="SPY", fast=2, slow=4)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    assert all(s.direction != SignalDirection.SHORT for s in signals)


def test_ema_has_no_warmup_nans_but_isna_guard_is_still_safe():
    # Unlike an SMA, `IndicatorEngine`'s EMA (`.ewm(adjust=False)`) is
    # defined from the very first row -- there is no NaN warmup period
    # to skip. `generate_signals()`'s `pd.isna()` check is therefore
    # defensive (protects against a future indicator swap), not load-
    # bearing today; this test documents that rather than assuming a
    # warmup gap that doesn't actually exist for EMA.
    candles = make_candles([100, 101, 102, 103, 104, 105])
    strategy = EMACrossStrategy(symbol="SPY", fast=2, slow=4)
    prepared = strategy.prepare(candles)

    assert prepared["ema_fast"].isna().sum() == 0
    assert prepared["ema_slow"].isna().sum() == 0

    # The first row has fast == slow == the first close (ewm's own
    # seeding behavior) -- correctly treated as "not crossed up" (a
    # strict `>`), not mistaken for a warmup gap.
    first_row = prepared.iloc[0]
    assert first_row["ema_fast"] == first_row["ema_slow"]
    signals = strategy.generate_signals(prepared)
    assert prepared.index[0] not in {s.timestamp for s in signals}


def test_confidence_is_fixed_and_configurable():
    closes = [110, 108, 106, 104, 102, 100, 105, 110, 115, 120]
    candles = make_candles(closes)
    strategy = EMACrossStrategy(symbol="SPY", fast=2, slow=4, confidence=0.42)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    assert signals  # sanity check
    assert all(s.confidence == 0.42 for s in signals)


def test_signal_metadata_includes_strategy_and_reason():
    closes = [110, 108, 106, 104, 102, 100, 105, 110, 115, 120]
    candles = make_candles(closes)
    strategy = EMACrossStrategy(symbol="SPY", fast=2, slow=4)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    assert signals
    for signal in signals:
        assert signal.symbol == "SPY"
        assert signal.metadata["strategy"] == "ema_cross"
        assert "crossed" in signal.metadata["reason"]


def test_runs_end_to_end_through_backtester_with_default_periods():
    # Enough candles for the default 12/26 periods to actually produce
    # non-NaN EMA values and at least a chance of a crossover.
    closes = [100 + (i % 10) - 5 + i * 0.3 for i in range(80)]
    candles = make_candles(closes)
    strategy = EMACrossStrategy(symbol="SPY")

    result = Backtester().run(strategy, candles)

    assert result.strategy_name == "ema_cross"
    assert isinstance(result.trades, list)
    assert isinstance(result.signals, list)
