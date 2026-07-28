"""Tests for the Market Regime Engine (src/regime).

Uses deliberately simple, deterministic synthetic price series (a
constant/flat series, a steady linear uptrend, a narrow-then-wide
volatility series) so expected outcomes can be reasoned about exactly,
rather than asserting on real market data where the "right" answer
isn't independently known.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.indicators.engine import IndicatorEngine
from src.regime.engine import REGIME_NAMES, MarketRegimeEngine


def make_flat_candles(rows: int = 200) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=rows, freq="D", name="timestamp")
    return pd.DataFrame(
        {
            "open": [100.0] * rows,
            "high": [100.5] * rows,
            "low": [99.5] * rows,
            "close": [100.0] * rows,
            "volume": [1000.0] * rows,
        },
        index=dates,
    )


def make_trending_candles(rows: int = 200) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=rows, freq="D", name="timestamp")
    close = [100.0 + i for i in range(rows)]
    return pd.DataFrame(
        {
            "open": [c - 0.2 for c in close],
            "high": [c + 0.5 for c in close],
            "low": [c - 0.5 for c in close],
            "close": close,
            "volume": [1000.0] * rows,
        },
        index=dates,
    )


def make_narrow_then_wide_candles(rows: int = 200, wide_from: int = 180) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=rows, freq="D", name="timestamp")
    ranges = [1.0 if i < wide_from else 20.0 for i in range(rows)]
    return pd.DataFrame(
        {
            "open": [100.0] * rows,
            "high": [100.0 + r / 2 for r in ranges],
            "low": [100.0 - r / 2 for r in ranges],
            "close": [100.0] * rows,
            "volume": [1000.0] * rows,
        },
        index=dates,
    )


# -- Shape / contract --------------------------------------------------


def test_score_returns_all_six_regime_columns():
    engine = MarketRegimeEngine(make_flat_candles())
    scores = engine.score()
    assert list(scores.columns) == REGIME_NAMES


def test_risk_axis_is_always_nan():
    engine = MarketRegimeEngine(make_trending_candles())
    scores = engine.score()
    assert scores["risk_on"].isna().all()
    assert scores["risk_off"].isna().all()


def test_trend_axis_scores_sum_to_one():
    engine = MarketRegimeEngine(make_trending_candles())
    scores = engine.score()
    valid = scores.dropna(subset=["trending", "ranging"])
    totals = (valid["trending"] + valid["ranging"]).round(9)
    assert (totals == 1.0).all()


def test_volatility_axis_scores_sum_to_one():
    engine = MarketRegimeEngine(make_narrow_then_wide_candles())
    scores = engine.score()
    valid = scores.dropna(subset=["volatile", "low_volatility"])
    totals = (valid["volatile"] + valid["low_volatility"]).round(9)
    assert (totals == 1.0).all()


def test_accepts_a_precomputed_indicator_engine():
    candles = make_flat_candles()
    indicator_engine = IndicatorEngine(candles)
    regime_engine = MarketRegimeEngine(candles, engine=indicator_engine)
    # Should not raise, and should produce the same shape as default construction.
    scores = regime_engine.score()
    assert list(scores.columns) == REGIME_NAMES


# -- Trend axis behavior -------------------------------------------------


def test_flat_price_series_is_ranging():
    engine = MarketRegimeEngine(make_flat_candles())
    scores = engine.score()
    dominant = engine.dominant(scores)

    last = dominant.iloc[-1]
    assert last["trend_regime"] == "ranging"
    assert scores["trending"].iloc[-1] == 0.0


def test_steady_uptrend_is_trending():
    engine = MarketRegimeEngine(make_trending_candles())
    scores = engine.score()
    dominant = engine.dominant(scores)

    last = dominant.iloc[-1]
    assert last["trend_regime"] == "trending"
    assert scores["trending"].iloc[-1] > 0.5


# -- Volatility axis behavior --------------------------------------------


def test_narrow_period_is_low_volatility():
    engine = MarketRegimeEngine(make_narrow_then_wide_candles())
    scores = engine.score()
    dominant = engine.dominant(scores)

    # Deep in the narrow-range period, well past warmup, before any
    # wide-range candles have entered the trailing window.
    row = dominant.iloc[150]
    assert row["volatility_regime"] == "low_volatility"
    assert scores["volatile"].iloc[150] == 0.0


def test_wide_period_is_volatile():
    engine = MarketRegimeEngine(make_narrow_then_wide_candles())
    scores = engine.score()
    dominant = engine.dominant(scores)

    last = dominant.iloc[-1]
    assert last["volatility_regime"] == "volatile"
    assert scores["volatile"].iloc[-1] > 0.5


# -- Risk axis (not yet computable) --------------------------------------


def test_risk_regime_is_unknown():
    engine = MarketRegimeEngine(make_flat_candles())
    dominant = engine.dominant()
    assert (dominant["risk_regime"] == "unknown").all()
