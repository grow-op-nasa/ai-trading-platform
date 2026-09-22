"""Tests for `src/ai/dataset.py` (Sprint 10, `DECISIONS.md` ADR-0043).

Network-free, sklearn-free.
"""

from __future__ import annotations

import random

import pandas as pd
import pytest

from src.ai.dataset import build_training_table
from src.ai.features import FEATURE_COLUMNS, FeatureSpec
from src.ai.labels import LabelSpec


def _make_candles(n: int = 220, seed: int = 7) -> pd.DataFrame:
    rng = random.Random(seed)
    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.uniform(-0.02, 0.021)))
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


def test_training_table_has_no_nan_features_or_labels():
    candles = _make_candles()
    table = build_training_table(candles, FeatureSpec(), LabelSpec(horizon_bars=5))
    assert not table.X.isna().any().any()
    assert not table.y.isna().any()


def test_training_table_x_and_y_share_the_same_index():
    candles = _make_candles()
    table = build_training_table(candles)
    assert table.X.index.equals(table.y.index)


def test_training_table_drops_warmup_and_horizon_tail_rows():
    candles = _make_candles(n=100)
    label_spec = LabelSpec(horizon_bars=5)
    table = build_training_table(candles, FeatureSpec(), label_spec)
    # Strictly fewer rows than the raw candles -- both warmup (feature
    # NaN) and horizon-tail (label NaN) rows must have been removed.
    assert len(table.X) < len(candles)
    # None of the final horizon_bars candle timestamps can survive --
    # their labels are undefined by construction.
    undefined_tail = candles.index[-label_spec.horizon_bars :]
    assert not any(ts in table.X.index for ts in undefined_tail)


def test_training_table_columns_match_feature_columns():
    candles = _make_candles()
    table = build_training_table(candles)
    assert list(table.X.columns) == list(FEATURE_COLUMNS)


def test_training_table_labels_are_a_subset_of_the_three_classes():
    candles = _make_candles()
    table = build_training_table(candles)
    assert set(table.y.unique()) <= {"LONG", "SHORT", "FLAT"}


def test_too_short_a_dataset_raises_a_clear_error():
    candles = _make_candles(n=5)  # far shorter than any indicator's warmup
    with pytest.raises(ValueError, match="no rows survive"):
        build_training_table(candles)


def test_training_table_exposes_the_builders_used():
    candles = _make_candles()
    feature_spec = FeatureSpec(rsi_period=10)
    label_spec = LabelSpec(horizon_bars=3)
    table = build_training_table(candles, feature_spec, label_spec)
    assert table.feature_builder.spec == feature_spec
    assert table.label_builder.spec == label_spec
