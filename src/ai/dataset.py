"""Training table assembly -- src/ai/dataset.py.

The one place `src.ai.features.FeatureBuilder` output and
`src.ai.labels.LabelBuilder` output are actually combined into an
`(X, y)` pair a model can train on (Sprint 10 spec, section 8's
`FeatureBuilder -> X`, `LabelBuilder -> y` split, composed without
either builder knowing about the other).

    from src.ai.dataset import build_training_table

    table = build_training_table(candles, feature_spec, label_spec)
    table.X          # DataFrame, FEATURE_COLUMNS, no NaN
    table.y          # Series, "LONG"/"SHORT"/"FLAT", same index as X

Row removal (Sprint 10 spec, section 9) happens exactly once, here, on
the *union* of two independent reasons a row can't be trained on:
indicator warmup (an early feature is still `NaN`) and horizon tail (a
late label has no future close to look at yet). Neither builder ever
fabricates a value to paper over the other's gap -- this module simply
keeps the rows where both sides are actually defined.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.ai.features import FeatureBuilder, FeatureSpec
from src.ai.labels import LabelBuilder, LabelSpec


@dataclass(frozen=True)
class TrainingTable:
    """`X`/`y` aligned on the same index, with every row usable for
    training or evaluation -- no warmup `NaN`, no undefined tail label.

    Args:
        X: features, columns = `src.ai.features.FEATURE_COLUMNS`.
        y: labels, values in `{"LONG", "SHORT", "FLAT"}`.
        feature_builder: the `FeatureBuilder` used to produce `X` --
            kept alongside the table so callers (`src.ai.training`)
            don't have to separately reconstruct it to read
            `feature_set_id()`/`spec`.
        label_builder: the `LabelBuilder` used to produce `y`, for the
            same reason.
    """

    X: pd.DataFrame
    y: pd.Series
    feature_builder: FeatureBuilder
    label_builder: LabelBuilder

    def __post_init__(self) -> None:
        if not self.X.index.equals(self.y.index):
            raise ValueError("X and y must share the exact same index")


def build_training_table(
    candles: pd.DataFrame,
    feature_spec: FeatureSpec | None = None,
    label_spec: LabelSpec | None = None,
) -> TrainingTable:
    """Build features and labels from `candles` and align them into one
    trainable table.

    Args:
        candles: canonical OHLCV candles (e.g. `CandleDataset.candles`).
        feature_spec: feature configuration; defaults to `FeatureSpec()`.
        label_spec: label configuration; defaults to `LabelSpec()`.

    Raises:
        ValueError: `candles` is missing a required column, or no rows
            survive warmup/horizon removal (e.g. `candles` is shorter
            than the longest indicator warmup plus `horizon_bars`).
    """
    feature_builder = FeatureBuilder(feature_spec)
    label_builder = LabelBuilder(label_spec)

    features = feature_builder.build(candles)
    labels = label_builder.build(candles)

    valid = features.notna().all(axis=1) & ~features.isin([float("inf"), float("-inf")]).any(
        axis=1
    ) & labels.notna()

    X = features.loc[valid].copy()
    y = labels.loc[valid].copy()

    if X.empty:
        raise ValueError(
            "no rows survive feature-warmup/label-horizon removal -- "
            "candles is too short for this feature_spec/label_spec combination"
        )

    return TrainingTable(X=X, y=y, feature_builder=feature_builder, label_builder=label_builder)
