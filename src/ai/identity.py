"""Model identity -- src/ai/identity.py.

Sprint 6/8 established strategy identity (`src.strategies.identity.
strategy_version`) and dataset identity (`src.utils.hashing.
dataframe_fingerprint`, `src.data.models.DatasetIdentity`) -- Sprint 10
(`DECISIONS.md`, ADR-0043) adds the equivalent for a trained model
(Sprint 10 spec, section 15). A model is not identified by a
human-readable name like `"logistic_regression_v1"`: two models with
that same name can have been trained on different datasets, different
feature/label configurations, or different hyperparameters, and a name
alone can't tell them apart (section 15's explicit example).

    from src.ai.identity import model_spec_id

    model_id = model_spec_id(
        model_type="logistic_regression",
        hyperparameters={"C": 1.0},
        feature_set_id=feature_builder.feature_set_id(),
        label_spec_id=label_builder.label_spec_id(),
        dataset_fingerprint=dataset.content_hash,
        train_start=X_train.index[0].isoformat(),
        train_end=X_train.index[-1].isoformat(),
        random_state=42,
    )

Deterministic (Sprint 10 spec, section 37): for identical inputs, the
same `model_id` every time, in any process, on any machine -- no
system time, no random component, nothing beyond the values actually
passed in. This is a **specification** identity (`model_spec_id`,
"what the model is supposed to be"), distinct from the **artifact**
content hash (`src.ai.artifacts`, "the exact bytes of the persisted
trained object") -- Sprint 10 spec, section 17 keeps the two separate
so a re-run that produces a bit-identical fit can be confirmed, not
just assumed, to match its own specification.
"""

from __future__ import annotations

import json
from typing import Any

from src.utils.hashing import sha256_hex


def model_spec_id(
    *,
    model_type: str,
    hyperparameters: dict[str, Any],
    feature_set_id: str,
    label_spec_id: str,
    dataset_fingerprint: str,
    train_start: str,
    train_end: str,
    random_state: int | None,
) -> str:
    """Deterministic hex digest identifying a model's full specification.

    Every argument that can vary between two otherwise-similar models
    is included -- deliberately not the trained coefficients themselves
    (that's `src.ai.artifacts`' job): two independent training runs
    with identical inputs and an identical `random_state` are the same
    *specification*, whether or not their fitted numbers turn out
    bit-identical across library/platform versions.

    Args:
        model_type: the registered `AIModel` implementation name
            (`src.ai.model.ModelConfig.model_type`).
        hyperparameters: the estimator's own hyperparameters.
        feature_set_id: `FeatureBuilder.feature_set_id()`'s output.
        label_spec_id: `LabelBuilder.label_spec_id()`'s output.
        dataset_fingerprint: the training dataset's content hash
            (`src.utils.hashing.dataframe_fingerprint`, typically
            `CandleDataset.content_hash`) -- the original market data
            identity, not merely a hash of the derived feature matrix
            (Sprint 10 spec, section 34).
        train_start / train_end: ISO-8601 timestamps bounding the
            training split actually used.
        random_state: the seed passed to the underlying estimator, if
            any.
    """
    payload = {
        "model_type": model_type,
        "hyperparameters": hyperparameters,
        "feature_set_id": feature_set_id,
        "label_spec_id": label_spec_id,
        "dataset_fingerprint": dataset_fingerprint,
        "train_start": train_start,
        "train_end": train_end,
        "random_state": random_state,
    }
    return sha256_hex(json.dumps(payload, sort_keys=True).encode("utf-8"))
