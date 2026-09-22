"""Model artifact persistence -- src/ai/artifacts.py.

Every trained model's fitted state (`AIModel.to_artifact()`) is saved
here, under a platform-controlled directory keyed by `model_id`
(Sprint 10 spec, section 16). `joblib` (already a transitive
scikit-learn dependency, pinned directly in `requirements.txt`) is the
serialization format -- the standard choice for a fitted scikit-learn
`Pipeline`, and simpler than hand-rolling a pickle-safe format for
Sprint 10's single model type.

    from src.ai.artifacts import ModelArtifactStore

    store = ModelArtifactStore()
    artifact_hash = store.save(model_id, model.to_artifact())
    artifact = store.load(model_id)   # the same dict, byte-identical

**Trusted-input-only, by design** (Sprint 10 spec, section 61): `load()`
only ever reads from this store's own controlled directory, keyed by a
`model_id` this platform itself generated
(`src.ai.identity.model_spec_id`) -- there is deliberately no method
here that accepts an arbitrary filesystem path. Both `joblib`/`pickle`
can execute arbitrary code on load, so the trust boundary that matters
is "this file was written by `ModelArtifactStore.save()`, by this
platform's own training pipeline" -- never "some path a caller
supplied."

Model binaries are runtime-generated state, not source -- `data/*` is
already `.gitignore`d (`DECISIONS.md`, ADR-0016's `data/experiments.db`
precedent), so `data/models/` inherits that without a `.gitignore`
change (Sprint 10 spec, section 73).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import joblib

DEFAULT_MODELS_DIR = Path("data/models")

_ARTIFACT_FILENAME = "model.joblib"


class ModelArtifactStore:
    """Saves/loads model artifacts under a single controlled directory.

    Args:
        base_dir: root directory for all artifacts, one subdirectory
            per `model_id`. Created on first use if it doesn't exist.
    """

    def __init__(self, base_dir: Path | str = DEFAULT_MODELS_DIR) -> None:
        self._base_dir = Path(base_dir)

    def artifact_path(self, model_id: str) -> Path:
        """The on-disk path `model_id`'s artifact would be saved to /
        loaded from -- does not imply the file exists."""
        return self._base_dir / model_id / _ARTIFACT_FILENAME

    def save(self, model_id: str, artifact: object) -> str:
        """Persist `artifact` (typically `AIModel.to_artifact()`'s
        output) under `model_id` and return its SHA-256 content hash.

        Args:
            model_id: the model's specification identity
                (`src.ai.identity.model_spec_id`) -- distinct from the
                artifact hash this method returns (Sprint 10 spec,
                section 17): `model_id` says what the model is supposed
                to be, the returned hash identifies the exact persisted
                bytes.
            artifact: anything `joblib.dump()` can serialize.
        """
        path = self.artifact_path(model_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(artifact, path)
        return _sha256_of_file(path)

    def load(self, model_id: str) -> object:
        """Load `model_id`'s previously-saved artifact.

        Raises:
            FileNotFoundError: no artifact was ever saved under
                `model_id` -- fails loudly rather than returning `None`
                or a fabricated empty model (Sprint 10 spec, section
                18: "retrieve by model_id" must be a real lookup).
        """
        path = self.artifact_path(model_id)
        if not path.exists():
            raise FileNotFoundError(
                f"no model artifact found for model_id={model_id!r} at {path} -- "
                f"it may never have been trained, or was trained against a "
                f"different ModelArtifactStore base_dir"
            )
        return joblib.load(path)

    def artifact_hash(self, model_id: str) -> str:
        """The current SHA-256 content hash of `model_id`'s saved
        artifact file, recomputed from disk (not cached) -- lets a
        caller confirm a stored artifact hasn't been tampered with or
        corrupted since `save()` returned its hash.

        Raises:
            FileNotFoundError: no artifact was ever saved under
                `model_id`.
        """
        path = self.artifact_path(model_id)
        if not path.exists():
            raise FileNotFoundError(f"no model artifact found for model_id={model_id!r}")
        return _sha256_of_file(path)


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
