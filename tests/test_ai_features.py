"""Tests for `src/ai/features.py` (Sprint 10, `DECISIONS.md` ADR-0043).

Network-free, sklearn-free -- `FeatureBuilder` only needs pandas and
`src.indicators`, both already exercised heavily by the rest of this
suite, so these tests run for real in every environment, sandbox
included (`tests/test_ai_model.py` and friends are the ones gated on
scikit-learn).
"""

from __future__ import annotations

import random

import pandas as pd
import pytest

from src.ai.features import FEATURE_COLUMNS, FeatureBuilder, FeatureSpec


def _make_candles(n: int = 220, seed: int = 42) -> pd.DataFrame:
    """A deterministic, non-trivial synthetic OHLCV series -- enough
    bars for every indicator's warmup to complete, enough variance for
    every feature to take on genuinely different values row to row."""
    rng = random.Random(seed)
    closes = [100.0]
    for _ in range(n - 1):
        pct = rng.uniform(-0.02, 0.021)
        closes.append(closes[-1] * (1 + pct))
    dates = pd.date_range("2023-01-01", periods=n, freq="D", name="timestamp")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes],
            "close": closes,
            "volume": [rng.uniform(800.0, 1200.0) for _ in range(n)],
        },
        index=dates,
    )


def test_feature_columns_match_the_published_constant():
    candles = _make_candles()
    features = FeatureBuilder().build(candles)
    assert list(features.columns) == list(FEATURE_COLUMNS)


def test_feature_index_is_aligned_to_candles():
    candles = _make_candles()
    features = FeatureBuilder().build(candles)
    assert features.index.equals(candles.index)


def test_warmup_rows_are_nan_and_later_rows_are_fully_defined():
    candles = _make_candles(n=220)
    features = FeatureBuilder().build(candles)

    # The very first row can't have a 1-bar return at all.
    assert pd.isna(features["return_1"].iloc[0])

    # Well past every indicator's own warmup (longest is MACD's slow
    # EMA + signal line, well under 60 bars), every feature must be a
    # real, finite number -- no lingering NaN, no silently-produced inf.
    tail = features.iloc[100:]
    assert not tail.isna().any().any()
    assert not (tail.abs() == float("inf")).any().any()


def test_same_input_produces_identical_features():
    candles = _make_candles()
    first = FeatureBuilder().build(candles)
    second = FeatureBuilder().build(candles)
    pd.testing.assert_frame_equal(first, second)


def test_features_are_past_only_truncating_the_future_does_not_change_the_past():
    # The decisive past-only check (Sprint 10 spec, section 39): every
    # feature at row t must be computable from data <= t alone. If that
    # holds, building features on a truncated series and on the full
    # series must agree exactly on every row the truncated series has.
    candles = _make_candles(n=220)
    truncated = candles.iloc[:150]

    full_features = FeatureBuilder().build(candles)
    truncated_features = FeatureBuilder().build(truncated)

    pd.testing.assert_frame_equal(
        full_features.loc[truncated.index],
        truncated_features,
    )


def test_missing_required_column_raises():
    candles = _make_candles().drop(columns=["volume"])
    with pytest.raises(ValueError, match="missing required column"):
        FeatureBuilder().build(candles)


def test_feature_set_id_is_deterministic():
    spec = FeatureSpec(rsi_period=10)
    assert FeatureBuilder(spec).feature_set_id() == FeatureBuilder(spec).feature_set_id()


def test_feature_set_id_is_sensitive_to_spec_changes():
    default_id = FeatureBuilder(FeatureSpec()).feature_set_id()
    changed_id = FeatureBuilder(FeatureSpec(rsi_period=21)).feature_set_id()
    assert default_id != changed_id


def test_feature_spec_rejects_wrong_number_of_return_periods():
    with pytest.raises(ValueError, match="return_periods"):
        FeatureSpec(return_periods=(1, 5))


def test_feature_spec_rejects_non_positive_periods():
    with pytest.raises(ValueError):
        FeatureSpec(rsi_period=0)
    with pytest.raises(ValueError):
        FeatureSpec(atr_period=-1)
