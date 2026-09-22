"""Tests for `src/ai/artifacts.py` and `src/ai/registry.py` (Sprint 10,
`DECISIONS.md` ADR-0043).

Gated on scikit-learn/joblib (see `tests/test_ai_model.py`'s module
docstring for why).
"""

from __future__ import annotations

import random

import pandas as pd
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("joblib")

from src.ai.artifacts import ModelArtifactStore
from src.ai.model import LogisticRegressionModel, ModelConfig
from src.ai.registry import ModelMetadata, ModelRegistry


def _make_fitted_model(seed: int = 1):
    rng = random.Random(seed)
    n = 60
    X = pd.DataFrame(
        {"f1": [rng.uniform(-1, 1) for _ in range(n)], "f2": [rng.uniform(-1, 1) for _ in range(n)]}
    )
    y = pd.Series(["LONG" if v > 0 else "SHORT" for v in X["f1"]])
    return LogisticRegressionModel(ModelConfig(random_state=seed)).fit(X, y), X


def _make_metadata(model_id: str, artifact_hash: str, feature_columns: list[str], classes: list[str]) -> ModelMetadata:
    return ModelMetadata(
        model_id=model_id,
        model_type="logistic_regression",
        hyperparameters={},
        random_state=1,
        feature_spec={
            "return_periods": [1, 5, 20],
            "ema_fast": 12,
            "ema_slow": 26,
            "rsi_period": 14,
            "atr_period": 14,
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal": 9,
            "volume_window": 20,
        },
        feature_set_id="feat-abc",
        feature_columns=feature_columns,
        label_spec={"horizon_bars": 5, "neutral_threshold": 0.01},
        label_spec_id="label-abc",
        classes=classes,
        dataset_fingerprint="dataset-abc",
        dataset_source="synthetic",
        symbol="TEST",
        interval="1d",
        train_start="2024-01-01T00:00:00",
        train_end="2024-06-01T00:00:00",
        validation_start=None,
        validation_end=None,
        test_start=None,
        test_end=None,
        artifact_hash=artifact_hash,
    )


# ---------------------------------------------------------------------------
# ModelArtifactStore
# ---------------------------------------------------------------------------


def test_save_and_load_artifact_round_trips(tmp_path):
    model, X = _make_fitted_model()
    store = ModelArtifactStore(tmp_path)
    artifact_hash = store.save("model-1", model.to_artifact())

    loaded = store.load("model-1")
    restored = LogisticRegressionModel.from_artifact(loaded, model.config)
    pd.testing.assert_frame_equal(model.predict_proba(X), restored.predict_proba(X))
    assert isinstance(artifact_hash, str) and len(artifact_hash) == 64  # sha256 hex


def test_artifact_hash_is_stable_across_reads(tmp_path):
    model, _ = _make_fitted_model()
    store = ModelArtifactStore(tmp_path)
    saved_hash = store.save("model-1", model.to_artifact())
    assert store.artifact_hash("model-1") == saved_hash


def test_loading_unknown_model_id_fails_clearly(tmp_path):
    store = ModelArtifactStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.load("does-not-exist")


def test_artifact_hash_of_missing_model_fails_clearly(tmp_path):
    store = ModelArtifactStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.artifact_hash("does-not-exist")


def test_two_different_fits_produce_different_artifact_hashes(tmp_path):
    model_a, _ = _make_fitted_model(seed=1)
    model_b, _ = _make_fitted_model(seed=2)
    store = ModelArtifactStore(tmp_path)
    hash_a = store.save("model-a", model_a.to_artifact())
    hash_b = store.save("model-b", model_b.to_artifact())
    assert hash_a != hash_b


# ---------------------------------------------------------------------------
# ModelRegistry
# ---------------------------------------------------------------------------


def test_save_and_get_metadata_round_trips(tmp_path):
    registry = ModelRegistry(tmp_path)
    metadata = _make_metadata("model-1", "hash-1", ["f1", "f2"], ["LONG", "SHORT"])
    registry.save_metadata(metadata)

    fetched = registry.get_metadata("model-1")
    assert fetched == metadata


def test_get_metadata_for_unknown_model_returns_none(tmp_path):
    registry = ModelRegistry(tmp_path)
    assert registry.get_metadata("does-not-exist") is None


def test_list_models_returns_every_saved_model(tmp_path):
    registry = ModelRegistry(tmp_path)
    registry.save_metadata(_make_metadata("model-1", "hash-1", ["f1"], ["LONG", "SHORT"]))
    registry.save_metadata(_make_metadata("model-2", "hash-2", ["f1"], ["LONG", "SHORT"]))

    listed_ids = {m.model_id for m in registry.list_models()}
    assert listed_ids == {"model-1", "model-2"}


def test_list_models_on_an_empty_registry_returns_empty_list(tmp_path):
    registry = ModelRegistry(tmp_path / "does-not-exist-yet")
    assert registry.list_models() == []


def test_load_model_returns_a_usable_model_and_its_metadata(tmp_path):
    fitted, X = _make_fitted_model()
    store = ModelArtifactStore(tmp_path)
    registry = ModelRegistry(tmp_path, artifact_store=store)
    artifact_hash = store.save("model-1", fitted.to_artifact())
    metadata = _make_metadata("model-1", artifact_hash, fitted.feature_columns, fitted.classes_)
    registry.save_metadata(metadata)

    loaded_model, loaded_metadata = registry.load_model("model-1")
    pd.testing.assert_frame_equal(fitted.predict_proba(X), loaded_model.predict_proba(X))
    assert loaded_metadata == metadata


def test_load_model_for_unregistered_id_fails_clearly(tmp_path):
    registry = ModelRegistry(tmp_path)
    with pytest.raises(KeyError):
        registry.load_model("does-not-exist")


def test_save_metadata_overwrites_rather_than_duplicates(tmp_path):
    registry = ModelRegistry(tmp_path)
    first = _make_metadata("model-1", "hash-1", ["f1"], ["LONG", "SHORT"])
    registry.save_metadata(first)
    second = _make_metadata("model-1", "hash-2", ["f1"], ["LONG", "SHORT"])
    registry.save_metadata(second)

    assert registry.get_metadata("model-1").artifact_hash == "hash-2"
    assert len(registry.list_models()) == 1
