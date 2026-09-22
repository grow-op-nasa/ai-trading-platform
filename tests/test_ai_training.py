"""Tests for `src/ai/training.py` (Sprint 10, `DECISIONS.md` ADR-0043).

Gated on scikit-learn/joblib (see `tests/test_ai_model.py`'s module
docstring).
"""

from __future__ import annotations

import random

import pandas as pd
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("joblib")

from src.ai.artifacts import ModelArtifactStore
from src.ai.features import FeatureSpec
from src.ai.labels import LabelSpec
from src.ai.model import ModelConfig
from src.ai.registry import ModelRegistry
from src.ai.splitting import SplitSpec
from src.ai.training import MLTrainingService, TrainingResult
from src.data.base import Interval
from src.data.canonical import canonicalize_candles
from src.data.models import CandleDataset, SessionPolicy, ValidationReport
from src.utils.hashing import dataframe_fingerprint


def _make_candles(n: int = 260, seed: int = 11) -> pd.DataFrame:
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


def _make_dataset(candles: pd.DataFrame, symbol: str = "TEST") -> CandleDataset:
    canonical = canonicalize_candles(candles)
    return CandleDataset(
        symbol=symbol,
        interval=Interval.DAY_1,
        provider="synthetic",
        candles=canonical,
        content_hash=dataframe_fingerprint(canonical),
        validation_report=ValidationReport(symbol=symbol, interval=Interval.DAY_1),
        session_policy=SessionPolicy.REGULAR,
        session_timezone="America/New_York",
        requested_start=canonical.index[0].date(),
        requested_end=canonical.index[-1].date(),
        dataset_start=canonical.index[0],
        dataset_end=canonical.index[-1],
    )


def _service(tmp_path) -> MLTrainingService:
    store = ModelArtifactStore(tmp_path)
    registry = ModelRegistry(tmp_path, artifact_store=store)
    return MLTrainingService(registry=registry, artifact_store=store)


def test_train_returns_a_structured_training_result(tmp_path):
    dataset = _make_dataset(_make_candles())
    result = _service(tmp_path).train(dataset)

    assert isinstance(result, TrainingResult)
    assert isinstance(result.model_id, str) and result.model_id
    assert isinstance(result.artifact_hash, str) and len(result.artifact_hash) == 64
    assert result.training_metrics["status"] == "ok"
    assert result.validation_metrics["status"] == "ok"
    assert result.test_metrics["status"] == "ok"
    assert result.dataset_fingerprint == dataset.content_hash
    assert result.symbol == "TEST"


def test_train_ranges_are_strictly_chronological(tmp_path):
    dataset = _make_dataset(_make_candles())
    result = _service(tmp_path).train(dataset)

    train_start, train_end = result.train_range
    val_start, val_end = result.validation_range
    test_start, test_end = result.test_range
    assert train_start < train_end < val_start <= val_end < test_start <= test_end


def test_train_persists_metadata_and_artifact_retrievable_by_model_id(tmp_path):
    dataset = _make_dataset(_make_candles())
    store = ModelArtifactStore(tmp_path)
    registry = ModelRegistry(tmp_path, artifact_store=store)
    service = MLTrainingService(registry=registry, artifact_store=store)

    result = service.train(dataset)

    metadata = registry.get_metadata(result.model_id)
    assert metadata is not None
    assert metadata.artifact_hash == result.artifact_hash
    assert metadata.dataset_fingerprint == dataset.content_hash

    model, loaded_metadata = registry.load_model(result.model_id)
    assert loaded_metadata.model_id == result.model_id
    assert model.feature_columns == result.feature_columns


def test_training_is_deterministic_for_identical_inputs(tmp_path):
    dataset = _make_dataset(_make_candles())
    config = ModelConfig(random_state=99)

    result_a = _service(tmp_path / "a").train(dataset, model_config=config)
    result_b = _service(tmp_path / "b").train(dataset, model_config=config)

    assert result_a.model_id == result_b.model_id
    assert result_a.artifact_hash == result_b.artifact_hash


def test_different_label_horizon_produces_a_different_model_id(tmp_path):
    dataset = _make_dataset(_make_candles())
    service = _service(tmp_path)
    result_5 = service.train(dataset, label_spec=LabelSpec(horizon_bars=5))
    result_20 = service.train(dataset, label_spec=LabelSpec(horizon_bars=20))
    assert result_5.model_id != result_20.model_id


def test_too_short_dataset_raises_clear_error(tmp_path):
    dataset = _make_dataset(_make_candles(n=10))
    with pytest.raises(ValueError):
        _service(tmp_path).train(dataset)


def test_custom_feature_and_split_spec_are_honored(tmp_path):
    dataset = _make_dataset(_make_candles())
    result = _service(tmp_path).train(
        dataset,
        feature_spec=FeatureSpec(rsi_period=21),
        split_spec=SplitSpec(0.7, 0.15, 0.15),
    )
    assert result.feature_spec["rsi_period"] == 21


# ---------------------------------------------------------------------------
# Walk-forward evaluation
# ---------------------------------------------------------------------------


def test_walk_forward_evaluate_returns_fold_results_without_persisting_models(tmp_path):
    dataset = _make_dataset(_make_candles(n=200))
    store = ModelArtifactStore(tmp_path)
    registry = ModelRegistry(tmp_path, artifact_store=store)
    service = MLTrainingService(registry=registry, artifact_store=store)

    folds = service.walk_forward_evaluate(dataset, min_train_bars=80, test_bars=20)

    assert len(folds) >= 2
    for fold in folds:
        assert fold.test_metrics["status"] == "ok"
    # Walk-forward is diagnostic only -- it must not register any model.
    assert registry.list_models() == []


def test_walk_forward_folds_train_on_expanding_windows(tmp_path):
    dataset = _make_dataset(_make_candles(n=200))
    folds = _service(tmp_path).walk_forward_evaluate(dataset, min_train_bars=80, test_bars=20)
    for earlier, later in zip(folds, folds[1:]):
        assert later.train_range[1] > earlier.train_range[1]
