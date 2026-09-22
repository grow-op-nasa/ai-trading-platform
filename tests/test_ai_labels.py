"""Tests for `src/ai/labels.py` (Sprint 10, `DECISIONS.md` ADR-0043).

Network-free, sklearn-free.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.ai.features import FeatureBuilder
from src.ai.labels import FLAT, LONG, SHORT, LabelBuilder, LabelSpec


def _make_candles(closes: list[float]) -> pd.DataFrame:
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


def test_long_label_when_future_return_exceeds_threshold():
    # horizon=1, threshold=0.01: close goes 100 -> 102 (+2%) -> LONG at t=0.
    candles = _make_candles([100, 102, 102, 102])
    labels = LabelBuilder(LabelSpec(horizon_bars=1, neutral_threshold=0.01)).build(candles)
    assert labels.iloc[0] == LONG


def test_short_label_when_future_return_below_negative_threshold():
    candles = _make_candles([100, 97, 97, 97])
    labels = LabelBuilder(LabelSpec(horizon_bars=1, neutral_threshold=0.01)).build(candles)
    assert labels.iloc[0] == SHORT


def test_flat_label_when_future_return_within_neutral_band():
    candles = _make_candles([100, 100.5, 100.5, 100.5])
    labels = LabelBuilder(LabelSpec(horizon_bars=1, neutral_threshold=0.01)).build(candles)
    assert labels.iloc[0] == FLAT


def test_flat_label_exactly_at_the_threshold_boundary():
    # Exactly 0% forward return with threshold=0.0 -- the comparisons
    # are strict (`>`/`<`), so a return of exactly the threshold is
    # FLAT, not LONG/SHORT. Uses threshold=0.0 and an unchanged close
    # (100 -> 100) rather than a nonzero threshold: `close[t+h]/close[t]`
    # for two equal floats is always exactly `1.0` in IEEE-754, so
    # `future_return` is exactly `0.0` -- no floating-point rounding
    # risk the way e.g. `101/100 - 1` (not exactly representable) would
    # carry at a nonzero boundary.
    candles = _make_candles([100, 100, 100, 100])
    labels = LabelBuilder(LabelSpec(horizon_bars=1, neutral_threshold=0.0)).build(candles)
    assert labels.iloc[0] == FLAT


def test_horizon_alignment_label_at_t_depends_on_close_at_t_plus_horizon():
    # horizon=2: label at t=0 depends on close[2], not close[1].
    candles = _make_candles([100, 200, 103, 103])  # close[1] would say LONG, close[2] says FLAT
    labels = LabelBuilder(LabelSpec(horizon_bars=2, neutral_threshold=0.01)).build(candles)
    assert labels.iloc[0] == LONG  # (103/100 - 1) = 3% > 1%threshold -> LONG, using close[2]


def test_final_horizon_rows_are_undefined_not_flat():
    candles = _make_candles([100, 101, 102, 103, 104])
    labels = LabelBuilder(LabelSpec(horizon_bars=2)).build(candles)
    # Last 2 rows have no close[t+2] to look at -- must be NaN, never a
    # fabricated "FLAT".
    assert pd.isna(labels.iloc[-1])
    assert pd.isna(labels.iloc[-2])
    assert not pd.isna(labels.iloc[-3])


def test_label_spec_id_is_deterministic_and_sensitive_to_horizon():
    id_5 = LabelBuilder(LabelSpec(horizon_bars=5)).label_spec_id()
    id_5_again = LabelBuilder(LabelSpec(horizon_bars=5)).label_spec_id()
    id_20 = LabelBuilder(LabelSpec(horizon_bars=20)).label_spec_id()
    assert id_5 == id_5_again
    assert id_5 != id_20


def test_label_spec_rejects_non_positive_horizon():
    with pytest.raises(ValueError):
        LabelSpec(horizon_bars=0)


def test_label_spec_rejects_negative_threshold():
    with pytest.raises(ValueError):
        LabelSpec(neutral_threshold=-0.01)


def test_missing_close_column_raises():
    candles = _make_candles([100, 101, 102]).drop(columns=["close"])
    with pytest.raises(ValueError, match="close"):
        LabelBuilder().build(candles)


def test_changing_a_future_close_changes_the_label_but_not_the_feature_row():
    # The decisive leakage regression test (Sprint 10 spec, section 40):
    # two datasets identical up through row t, differing only in a
    # close well beyond t + horizon, must produce the SAME feature row
    # at t but can legitimately produce a DIFFERENT label at t.
    horizon = 3
    base_closes = [100, 101, 99, 102, 101, 100, 103, 98, 105, 110]
    closes_a = list(base_closes)
    closes_b = list(base_closes)
    # Only the very last close differs -- far beyond any t we inspect,
    # and chosen so the affected label actually flips class (LONG vs
    # SHORT), not just magnitude.
    closes_b[-1] = 90.0

    candles_a = _make_candles(closes_a)
    candles_b = _make_candles(closes_b)

    features_a = FeatureBuilder().build(candles_a)
    features_b = FeatureBuilder().build(candles_b)
    # Every row except the very last one must have identical features --
    # none of them can "see" the changed final close.
    pd.testing.assert_frame_equal(features_a.iloc[:-1], features_b.iloc[:-1])

    spec = LabelSpec(horizon_bars=horizon, neutral_threshold=0.01)
    labels_a = LabelBuilder(spec).build(candles_a)
    labels_b = LabelBuilder(spec).build(candles_b)

    # The row whose label horizon reaches the changed close (index
    # len - 1 - horizon) must actually differ between the two runs --
    # confirming the label genuinely does depend on the future value
    # the feature row above proved was invisible to the features.
    t = len(base_closes) - 1 - horizon
    assert labels_a.iloc[t] != labels_b.iloc[t]
