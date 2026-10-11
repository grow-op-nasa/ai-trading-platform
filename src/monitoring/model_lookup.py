"""Model provenance lookup -- src/monitoring/model_lookup.py (ADR-0050).

Builds the `ModelLookup` the strategy/model compatibility check needs
from the existing `ModelRegistry` -- read-only: it reads saved metadata
and re-hashes the artifact file, never loads, trains or alters a model.
`src.ai.registry` is imported lazily so the deterministic monitor works
(reporting the check NOT_EVALUABLE) on installs without joblib.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from src.monitoring.state import ModelInfo, ModelLookup


def registry_model_lookup(base_dir: Path | str | None = None) -> ModelLookup | None:
    """A lookup over the real `ModelRegistry`, or `None` if the ML
    dependencies are not installed."""
    try:
        from src.ai.artifacts import ModelArtifactStore
        from src.ai.registry import DEFAULT_MODELS_DIR, ModelRegistry
    except ImportError:
        return None
    root = Path(base_dir) if base_dir is not None else Path(DEFAULT_MODELS_DIR)
    registry = ModelRegistry(root)
    store = ModelArtifactStore(root)

    def lookup(model_id: str) -> ModelInfo | None:
        metadata = registry.get_metadata(model_id)
        if metadata is None:
            return None
        path = store.artifact_path(model_id)
        recomputed = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        return ModelInfo(
            model_id=model_id,
            artifact_hash=metadata.artifact_hash,
            recomputed_artifact_hash=recomputed if recomputed is not None else "<artifact file missing>",
            symbol=metadata.symbol,
            interval=metadata.interval,
        )

    return lookup
