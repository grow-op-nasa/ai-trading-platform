"""The Model Registry -- src/ai/registry.py.

The AI-layer counterpart to `src.experiments.registry.ExperimentRegistry`
(Sprint 10 spec, section 18) -- answers "which exact model artifact
produced this experiment?" Deliberately its own, much smaller
mechanism rather than a second SQLite schema bolted onto
`ExperimentRegistry`: one model is one small, self-contained metadata
record plus one artifact file, so a directory of
`{model_id}/metadata.json` + `{model_id}/model.joblib` pairs (via
`src.ai.artifacts.ModelArtifactStore`) is the whole implementation
(Sprint 10 spec, section 18: "do not add an unnecessarily large
database abstraction").

    from src.ai.registry import ModelRegistry

    registry = ModelRegistry()
    registry.save_metadata(metadata)
    model, metadata = registry.load_model(model_id)   # frozen, never trains

No dependency on `src.broker`/`src.execution` anywhere in this module
(Sprint 10 spec, section 19) -- a trained model is useful for research
whether or not any broker is ever configured; an architecture test
(`tests/test_architecture.py`) checks this the same way it checks
`src.portfolio`'s own independence (`DECISIONS.md`, ADR-0031).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ai.artifacts import DEFAULT_MODELS_DIR, ModelArtifactStore
from src.ai.model import AIModel, ModelConfig, model_class_for

_METADATA_FILENAME = "metadata.json"


@dataclass(frozen=True)
class ModelMetadata:
    """Everything needed to answer Sprint 10 spec section 31's
    provenance questions for one trained model: which implementation,
    which hyperparameters, which feature/label configuration, which
    exact dataset, which training/validation/test ranges, and which
    persisted artifact.

    Args:
        model_id: this model's specification identity
            (`src.ai.identity.model_spec_id`).
        model_type: the registered `AIModel` implementation name.
        hyperparameters: the estimator's own hyperparameters.
        random_state: the seed passed at training time, if any.
        feature_spec: `FeatureSpec.to_dict()` -- the exact feature
            configuration this model expects at inference time.
        feature_set_id: `FeatureBuilder.feature_set_id()`'s output.
        feature_columns: the exact, ordered column names the model was
            fit on (Sprint 10 spec, section 60's inference-time check).
        label_spec: `LabelSpec.to_dict()`.
        label_spec_id: `LabelBuilder.label_spec_id()`'s output.
        classes: the class labels this model predicts among, in
            `predict_proba()`'s column order.
        dataset_fingerprint: the training dataset's content hash
            (Sprint 10 spec, section 34 -- the original market dataset
            identity, not a hash of the derived feature matrix).
        dataset_source: where the training candles came from (e.g.
            `"yfinance"`).
        symbol / interval: the instrument/timeframe trained on.
        train_start / train_end: ISO-8601 bounds of the training split.
        validation_start / validation_end: ISO-8601 bounds of the
            validation split -- `None`/`None` if that split was empty.
        test_start / test_end: ISO-8601 bounds of the test split --
            `None`/`None` if that split was empty.
        artifact_hash: SHA-256 content hash of the persisted artifact
            file (`src.ai.artifacts.ModelArtifactStore.save()`'s
            return value) -- distinct from `model_id` (Sprint 10 spec,
            section 17).
        created_at: ISO-8601 UTC timestamp this metadata was saved.
    """

    model_id: str
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
    train_start: str
    train_end: str
    validation_start: str | None
    validation_end: str | None
    test_start: str | None
    test_end: str | None
    artifact_hash: str
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_model_config(self) -> ModelConfig:
        """Rebuild the `ModelConfig` this model was trained with --
        what `AIModel.from_artifact()` needs alongside the artifact
        itself to reconstruct a usable instance."""
        return ModelConfig(
            model_type=self.model_type,
            hyperparameters=dict(self.hyperparameters),
            random_state=self.random_state if self.random_state is not None else 42,
        )


class ModelRegistry:
    """Filesystem-backed store of `ModelMetadata`, one JSON file per
    model, alongside each model's `ModelArtifactStore`-managed artifact.

    Args:
        base_dir: root directory, shared with `ModelArtifactStore`
            unless `artifact_store` is given explicitly.
        artifact_store: override the artifact store used for
            `load_model()`/`get_artifact_hash()`. Defaults to a
            `ModelArtifactStore(base_dir)`.
    """

    def __init__(
        self,
        base_dir: Path | str = DEFAULT_MODELS_DIR,
        artifact_store: ModelArtifactStore | None = None,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._artifact_store = artifact_store or ModelArtifactStore(self._base_dir)

    def _metadata_path(self, model_id: str) -> Path:
        return self._base_dir / model_id / _METADATA_FILENAME

    def save_metadata(self, metadata: ModelMetadata) -> None:
        """Persist `metadata` under its own `model_id`. Overwrites any
        metadata previously saved under the same id (matches
        `ExperimentRegistry.save_spec()`'s `INSERT OR REPLACE`
        convention)."""
        path = self._metadata_path(metadata.model_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(metadata), indent=2, sort_keys=True))

    def get_metadata(self, model_id: str) -> ModelMetadata | None:
        """`model_id`'s saved metadata, or `None` if nothing was ever
        saved under it -- an explicit "nothing here", not an exception,
        matching `ExperimentRegistry.get_spec()`'s own convention."""
        path = self._metadata_path(model_id)
        if not path.exists():
            return None
        return ModelMetadata(**json.loads(path.read_text()))

    def list_models(self) -> list[ModelMetadata]:
        """Every model with saved metadata, ordered by `created_at`
        ascending (oldest first) -- `[]` if none have ever been saved."""
        if not self._base_dir.exists():
            return []
        records = []
        for child in sorted(self._base_dir.iterdir()):
            metadata_path = child / _METADATA_FILENAME
            if metadata_path.exists():
                records.append(ModelMetadata(**json.loads(metadata_path.read_text())))
        return sorted(records, key=lambda m: m.created_at)

    def load_model(self, model_id: str) -> tuple[AIModel, ModelMetadata]:
        """Load `model_id`'s fitted model and its metadata together --
        the one call `src.strategies.ai_signal.AISignalStrategy` needs
        at construction time. Never trains; purely restores
        already-fitted state (Sprint 10 spec, section 24: "it must not
        train the model itself").

        Raises:
            KeyError: no metadata is saved under `model_id`.
            FileNotFoundError: metadata exists but its artifact file is
                missing (an inconsistent registry state -- surfaced
                loudly rather than silently substituting a different
                model).
        """
        metadata = self.get_metadata(model_id)
        if metadata is None:
            raise KeyError(
                f"no model registered under model_id={model_id!r} -- "
                f"it may never have been trained, or the registry's base_dir "
                f"doesn't match where it was trained"
            )
        artifact = self._artifact_store.load(model_id)
        model_cls = model_class_for(metadata.model_type)
        model = model_cls.from_artifact(artifact, metadata.to_model_config())
        return model, metadata
