"""The ML training service -- src/ai/training.py.

The one application-facing entry point Sprint 10's whole chain funnels
through (Sprint 10 spec, section 20) -- not a Streamlit page, not a
strategy, not a script with the steps inlined:

    MarketDataService.get_dataset()
        -> MLTrainingService.train()
            -> features, labels, chronological split, fit, evaluate,
               persist artifact + metadata
            -> TrainingResult

    from src.ai.training import MLTrainingService

    result = MLTrainingService().train(dataset)
    result.model_id            # what src.strategies.ai_signal.AISignalStrategy loads
    result.test_metrics        # classification quality, out-of-sample

`train()` only ever consumes a `CandleDataset` (`src.data.models`) --
never `yfinance` directly (Sprint 10 spec, section 33; enforced by
`tests/test_architecture.py`) -- and only ever fits the model on the
training split `src.ai.splitting.chronological_split()` produces,
never on validation or test data (section 38).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from src.ai.artifacts import DEFAULT_MODELS_DIR, ModelArtifactStore
from src.ai.dataset import build_training_table
from src.ai.evaluation import classification_metrics
from src.ai.features import FeatureSpec
from src.ai.identity import model_spec_id
from src.ai.labels import LabelSpec
from src.ai.model import ModelConfig, build_model
from src.ai.registry import ModelMetadata, ModelRegistry
from src.ai.splitting import SplitSpec, chronological_split, walk_forward_splits
from src.data.models import CandleDataset

_EMPTY_SPLIT_METRICS = {"status": "undefined", "reason": "empty evaluation split", "n_samples": 0}


@dataclass(frozen=True)
class WalkForwardFoldResult:
    """One expanding-window fold's out-of-sample classification result
    (Sprint 10 spec, section 12) -- a fresh model is fit per fold, on
    that fold's own training window only; nothing here reuses another
    fold's fitted model or peeks at another fold's test window."""

    fold: int
    train_range: tuple[str, str]
    test_range: tuple[str, str]
    test_metrics: dict


@dataclass(frozen=True)
class TrainingResult:
    """Everything `MLTrainingService.train()` produces (Sprint 10 spec,
    section 21) -- the return value a caller (a script, a notebook, a
    future CLI command) actually works with; `ModelRegistry`/
    `ModelArtifactStore` hold the same information for later retrieval
    by `model_id`, this is the immediate, structured answer.

    Distinguishes three different claims that must never be conflated
    (Sprint 10 spec, section 75): `training_metrics` describes fit
    quality on data the model has seen; `validation_metrics` and
    `test_metrics` describe genuinely out-of-sample classification
    quality (`test_metrics` strictly more so, since nothing about
    `model_config`/`feature_spec`/`label_spec` may legally have been
    tuned against it -- Sprint 10 spec, section 57). None of the three
    is a trading-performance claim; that requires running the resulting
    `AISignalStrategy` through `Backtester` + `AnalyticsService`
    separately (section 58).
    """

    model_id: str
    artifact_hash: str
    model_type: str
    hyperparameters: dict[str, Any]
    random_state: int | None
    feature_spec: dict[str, Any]
    feature_set_id: str
    feature_columns: list[str]
    label_spec: dict[str, Any]
    label_spec_id: str
    classes: list[str]
    dataset_fingerprint: str
    dataset_source: str
    symbol: str
    interval: str
    train_range: tuple[str, str]
    validation_range: tuple[str, str] | None
    test_range: tuple[str, str] | None
    training_metrics: dict
    validation_metrics: dict
    test_metrics: dict

    def to_dict(self) -> dict:
        return asdict(self)


class MLTrainingService:
    """Runs the full feature -> label -> split -> fit -> evaluate ->
    persist pipeline for one model.

    Args:
        registry: where metadata is saved. Defaults to a
            `ModelRegistry()` at `src.ai.artifacts.DEFAULT_MODELS_DIR`.
        artifact_store: where the fitted artifact is saved. Defaults to
            a `ModelArtifactStore()` at the same directory. Sharing the
            default directory between the two is deliberate -- a
            `model_id` looked up in one is always findable in the
            other.
    """

    def __init__(
        self,
        registry: ModelRegistry | None = None,
        artifact_store: ModelArtifactStore | None = None,
    ) -> None:
        self._artifact_store = artifact_store or ModelArtifactStore(DEFAULT_MODELS_DIR)
        self._registry = registry or ModelRegistry(
            DEFAULT_MODELS_DIR, artifact_store=self._artifact_store
        )

    def train(
        self,
        dataset: CandleDataset,
        *,
        feature_spec: FeatureSpec | None = None,
        label_spec: LabelSpec | None = None,
        model_config: ModelConfig | None = None,
        split_spec: SplitSpec | None = None,
    ) -> TrainingResult:
        """Train one model against `dataset` and persist it.

        Args:
            dataset: the canonical candle dataset to train against
                (`MarketDataService.get_dataset()`, or an equivalent
                hand-built `CandleDataset` in tests) -- never a raw
                DataFrame or a `yfinance` call.
            feature_spec / label_spec / model_config: configuration;
                each defaults to its own dataclass default if omitted.
            split_spec: chronological train/validation/test
                proportions; defaults to `SplitSpec()`. `purge_bars`
                for the split is always `label_spec.horizon_bars` --
                not independently configurable here, since a smaller
                purge would silently reintroduce the exact leakage
                `src.ai.splitting` exists to prevent.

        Raises:
            ValueError: `dataset.candles` is too short to produce a
                non-empty training split for the given `feature_spec`/
                `label_spec`/`split_spec` combination.
        """
        feature_spec = feature_spec or FeatureSpec()
        label_spec = label_spec or LabelSpec()
        model_config = model_config or ModelConfig()
        split_spec = split_spec or SplitSpec()

        table = build_training_table(dataset.candles, feature_spec, label_spec)
        split = chronological_split(
            table.X.index, split_spec, purge_bars=label_spec.horizon_bars
        )

        if len(split.train) == 0:
            raise ValueError(
                "the training split is empty -- dataset is too short for this "
                "feature_spec/label_spec/split_spec combination"
            )

        X_train, y_train = table.X.loc[split.train], table.y.loc[split.train]
        X_val, y_val = table.X.loc[split.validation], table.y.loc[split.validation]
        X_test, y_test = table.X.loc[split.test], table.y.loc[split.test]

        model = build_model(model_config)
        model.fit(X_train, y_train)
        classes = tuple(model.classes_)

        training_metrics = classification_metrics(
            y_train, model.predict(X_train), model.predict_proba(X_train), classes=classes
        )
        validation_metrics = (
            classification_metrics(
                y_val, model.predict(X_val), model.predict_proba(X_val), classes=classes
            )
            if len(X_val) > 0
            else dict(_EMPTY_SPLIT_METRICS)
        )
        test_metrics = (
            classification_metrics(
                y_test, model.predict(X_test), model.predict_proba(X_test), classes=classes
            )
            if len(X_test) > 0
            else dict(_EMPTY_SPLIT_METRICS)
        )

        train_start, train_end = _iso_range(X_train.index)
        validation_range = _iso_range(X_val.index) if len(X_val) > 0 else (None, None)
        test_range = _iso_range(X_test.index) if len(X_test) > 0 else (None, None)

        model_id = model_spec_id(
            model_type=model_config.model_type,
            hyperparameters=dict(model_config.hyperparameters),
            feature_set_id=table.feature_builder.feature_set_id(),
            label_spec_id=table.label_builder.label_spec_id(),
            dataset_fingerprint=dataset.content_hash,
            train_start=train_start,
            train_end=train_end,
            random_state=model_config.random_state,
        )
        artifact_hash = self._artifact_store.save(model_id, model.to_artifact())

        metadata = ModelMetadata(
            model_id=model_id,
            model_type=model_config.model_type,
            hyperparameters=dict(model_config.hyperparameters),
            random_state=model_config.random_state,
            feature_spec=feature_spec.to_dict(),
            feature_set_id=table.feature_builder.feature_set_id(),
            feature_columns=model.feature_columns,
            label_spec=label_spec.to_dict(),
            label_spec_id=table.label_builder.label_spec_id(),
            classes=list(classes),
            dataset_fingerprint=dataset.content_hash,
            dataset_source=dataset.provider,
            symbol=dataset.symbol,
            interval=dataset.interval.value,
            train_start=train_start,
            train_end=train_end,
            validation_start=validation_range[0],
            validation_end=validation_range[1],
            test_start=test_range[0],
            test_end=test_range[1],
            artifact_hash=artifact_hash,
        )
        self._registry.save_metadata(metadata)

        return TrainingResult(
            model_id=model_id,
            artifact_hash=artifact_hash,
            model_type=model_config.model_type,
            hyperparameters=dict(model_config.hyperparameters),
            random_state=model_config.random_state,
            feature_spec=feature_spec.to_dict(),
            feature_set_id=table.feature_builder.feature_set_id(),
            feature_columns=model.feature_columns,
            label_spec=label_spec.to_dict(),
            label_spec_id=table.label_builder.label_spec_id(),
            classes=list(classes),
            dataset_fingerprint=dataset.content_hash,
            dataset_source=dataset.provider,
            symbol=dataset.symbol,
            interval=dataset.interval.value,
            train_range=(train_start, train_end),
            validation_range=validation_range if validation_range != (None, None) else None,
            test_range=test_range if test_range != (None, None) else None,
            training_metrics=training_metrics,
            validation_metrics=validation_metrics,
            test_metrics=test_metrics,
        )

    def walk_forward_evaluate(
        self,
        dataset: CandleDataset,
        *,
        min_train_bars: int,
        test_bars: int,
        feature_spec: FeatureSpec | None = None,
        label_spec: LabelSpec | None = None,
        model_config: ModelConfig | None = None,
        max_folds: int | None = None,
    ) -> list[WalkForwardFoldResult]:
        """Expanding-window walk-forward evaluation (Sprint 10 spec,
        section 12) -- diagnostic only, not a hyperparameter search
        (section 12: "do not create a hyperparameter optimizer") and
        not artifact-producing: each fold fits its own throwaway model
        purely to measure out-of-sample classification quality across
        time, nothing is persisted to `ModelRegistry`/
        `ModelArtifactStore` by this method.

        Args:
            dataset: same contract as `train()`.
            min_train_bars: fold 0's training window size.
            test_bars: each fold's evaluation window size.
            max_folds: cap on the number of folds; `None` runs every
                fold that fits.

        Returns:
            `[]` if `dataset` is too short to produce even one fold --
            not an error.
        """
        feature_spec = feature_spec or FeatureSpec()
        label_spec = label_spec or LabelSpec()
        model_config = model_config or ModelConfig()

        table = build_training_table(dataset.candles, feature_spec, label_spec)
        folds = walk_forward_splits(
            table.X.index,
            min_train_bars=min_train_bars,
            test_bars=test_bars,
            purge_bars=label_spec.horizon_bars,
            max_folds=max_folds,
        )

        results = []
        for fold in folds:
            X_train, y_train = table.X.loc[fold.train_index], table.y.loc[fold.train_index]
            X_test, y_test = table.X.loc[fold.test_index], table.y.loc[fold.test_index]

            model = build_model(model_config)
            model.fit(X_train, y_train)
            classes = tuple(model.classes_)
            metrics = classification_metrics(
                y_test, model.predict(X_test), model.predict_proba(X_test), classes=classes
            )
            results.append(
                WalkForwardFoldResult(
                    fold=fold.fold,
                    train_range=_iso_range(fold.train_index),
                    test_range=_iso_range(fold.test_index),
                    test_metrics=metrics,
                )
            )
        return results


def _iso_range(index: pd.Index) -> tuple[str, str]:
    return index[0].isoformat(), index[-1].isoformat()
