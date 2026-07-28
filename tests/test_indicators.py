"""Tests for the Indicator Engine (src/indicators).

Each formula is checked against an independent reference calculation
(a plain Python loop, not a re-statement of the pandas call in
formulas.py) so a bug in the vectorized implementation would actually
be caught rather than the test just re-deriving the same expression.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.indicators.engine import IndicatorEngine
from src.indicators.registry import UnknownIndicatorError


def make_candles() -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=10, freq="D", name="timestamp")
    return pd.DataFrame(
        {
            "open": [10, 11, 12, 13, 14, 15, 16, 15, 14, 13],
            "high": [11, 12, 13, 14, 15, 16, 17, 16, 15, 14],
            "low": [9, 10, 11, 12, 13, 14, 15, 14, 13, 12],
            "close": [10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 15.5, 14.5, 13.5],
            "volume": [100, 110, 120, 130, 140, 150, 160, 150, 140, 130],
        },
        index=dates,
    ).astype(float)


# -- Engine plumbing ---------------------------------------------------


def test_engine_rejects_candles_missing_columns():
    bad = pd.DataFrame({"open": [1], "close": [1]})
    with pytest.raises(ValueError):
        IndicatorEngine(bad)


def test_unknown_indicator_raises():
    engine = IndicatorEngine(make_candles())
    with pytest.raises(UnknownIndicatorError):
        engine.calculate("NOT_A_REAL_INDICATOR")


def test_calculate_is_case_insensitive():
    engine = IndicatorEngine(make_candles())
    result_upper = engine.calculate("SMA", period=3)
    result_lower = engine.calculate("sma", period=3)
    pd.testing.assert_series_equal(result_upper, result_lower)


def test_available_indicators_lists_expected_names():
    names = IndicatorEngine.available_indicators()
    for expected in ["SMA", "EMA", "RSI", "ATR", "MACD", "VWAP"]:
        assert expected in names


# -- SMA -----------------------------------------------------------------


def test_sma_matches_manual_rolling_mean():
    candles = make_candles()
    engine = IndicatorEngine(candles)

    result = engine.calculate("SMA", period=3)

    close = candles["close"].tolist()
    expected = [None, None] + [
        sum(close[i - 2 : i + 1]) / 3 for i in range(2, len(close))
    ]
    for actual, exp in zip(result.tolist(), expected):
        if exp is None:
            assert np.isnan(actual)
        else:
            assert actual == pytest.approx(exp)


# -- EMA -----------------------------------------------------------------


def test_ema_matches_manual_recurrence():
    candles = make_candles()
    engine = IndicatorEngine(candles)
    period = 3
    alpha = 2 / (period + 1)

    result = engine.calculate("EMA", period=period)

    close = candles["close"].tolist()
    expected = [close[0]]
    for value in close[1:]:
        expected.append(alpha * value + (1 - alpha) * expected[-1])

    for actual, exp in zip(result.tolist(), expected):
        assert actual == pytest.approx(exp)


# -- ATR -----------------------------------------------------------------


def test_atr_matches_manual_true_range():
    candles = make_candles()
    engine = IndicatorEngine(candles)
    period = 3

    result = engine.calculate("ATR", period=period)

    highs = candles["high"].tolist()
    lows = candles["low"].tolist()
    closes = candles["close"].tolist()
    true_ranges = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)

    expected = [None, None] + [
        sum(true_ranges[i - 2 : i + 1]) / 3 for i in range(2, len(true_ranges))
    ]
    for actual, exp in zip(result.tolist(), expected):
        if exp is None:
            assert np.isnan(actual)
        else:
            assert actual == pytest.approx(exp)


# -- VWAP ------------------------------------------------------------------


def test_vwap_matches_manual_cumulative_calc():
    candles = make_candles()
    engine = IndicatorEngine(candles)

    result = engine.calculate("VWAP")

    highs = candles["high"].tolist()
    lows = candles["low"].tolist()
    closes = candles["close"].tolist()
    volumes = candles["volume"].tolist()

    cum_vol = 0.0
    cum_turnover = 0.0
    expected = []
    for h, l, c, v in zip(highs, lows, closes, volumes):
        typical = (h + l + c) / 3
        cum_vol += v
        cum_turnover += typical * v
        expected.append(cum_turnover / cum_vol)

    for actual, exp in zip(result.tolist(), expected):
        assert actual == pytest.approx(exp)


# -- RSI -----------------------------------------------------------------


def test_rsi_is_bounded_between_0_and_100():
    candles = make_candles()
    engine = IndicatorEngine(candles)

    result = engine.calculate("RSI", period=3)

    valid = result.dropna()
    assert (valid >= 0).all()
    assert (valid <= 100).all()


def test_rsi_is_100_when_price_only_rises():
    dates = pd.date_range("2024-01-01", periods=10, freq="D", name="timestamp")
    rising_close = [10.0 + i for i in range(10)]
    candles = pd.DataFrame(
        {
            "open": rising_close,
            "high": [c + 0.5 for c in rising_close],
            "low": [c - 0.5 for c in rising_close],
            "close": rising_close,
            "volume": [100.0] * 10,
        },
        index=dates,
    )
    engine = IndicatorEngine(candles)

    result = engine.calculate("RSI", period=3)

    valid = result.dropna()
    assert all(v == pytest.approx(100.0) for v in valid.tolist())


# -- MACD ------------------------------------------------------------------


def test_macd_returns_macd_signal_histogram_columns():
    candles = make_candles()
    engine = IndicatorEngine(candles)

    result = engine.calculate("MACD", fast=3, slow=5, signal=2)

    assert list(result.columns) == ["macd", "signal", "histogram"]
    # histogram is always macd - signal, by construction
    diff = (result["macd"] - result["signal"] - result["histogram"]).abs()
    assert (diff < 1e-9).all()
