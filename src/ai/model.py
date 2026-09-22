"""The model abstraction -- src/ai/model.py.

`AIModel` is the seam that lets `LogisticRegression` work today and a
`RandomForest`/`GradientBoosting`/neural/LLM-backed model work later
without rewriting `src.ai.training.MLTrainingService` or
`src.strategies.ai_signal.AISignalStrategy` (Sprint 10 spec, sections
13, 64). It knows about features and class labels; it does not, and
must never, know about orders, brokers, portfolios, or risk (section
13) -- exactly the same posture `Strategy`/`BaseStrategy`
(`src/strategies/base.py`, `sdk.py`) already take toward the rest of
the platform.

    from src.ai.model import ModelConfig, LogisticRegressionModel

    model = LogisticRegressionModel(ModelConfig())
    model.fit(X_train, y_train)
    model.predict(X_test)          # array of "LONG"/"SHORT"/"FLAT"
    model.predict_proba(X_test)    # DataFrame, columns = classes

Uses scikit-learn (already a project dependency, `requirements.txt`)
-- not a new heavy ML stack (Sprint 10 spec, section 3). The
`StandardScaler -> LogisticRegression` pipeline is built once per
`ModelConfig` and fit exactly once, on whatever `X`/`y` `fit()` is
given -- `src.ai.training.MLTrainingService` is what's responsible for
only ever passing it the training split (Sprint 10 spec, section 6);
this class has no idea a validation or test split exists.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

DEFAULT_MODEL_TYPE = "logistic_regression"


@dataclass(frozen=True)
class ModelConfig:
    """Explicit model configuration (Sprint 10 spec, section 14) --
    every trained model's hyperparameters live here, never in an
    arbitrary global constant.

    Args:
        model_type: registered name (`register_model_type()`) of which
            `AIModel` implementation to build -- `"logistic_regression"`
            by default, the only implementation Sprint 10 ships
            (section 3: no model zoo).
        hyperparameters: keyword arguments forwarded to the underlying
            estimator (e.g. `{"C": 1.0, "max_iter": 1000}` for
            `LogisticRegression`). Defaults to `{}`, which uses
            scikit-learn's own defaults plus this module's
            `random_state`.
        random_state: seed forwarded to the underlying estimator
            wherever it accepts one (Sprint 10 spec, section 37:
            "use explicit random states... do not rely on global
            random state"). `42` is an arbitrary but fixed default --
            what matters is that it's explicit and recorded, not its
            particular value.
    """

    model_type: str = DEFAULT_MODEL_TYPE
    hyperparameters: dict[str, Any] = field(default_factory=dict)
    random_state: int = 42

    def to_dict(self) -> dict:
        return {
            "model_type": self.model_type,
            "hyperparameters": dict(self.hyperparameters),
            "random_state": self.random_state,
        }


class AIModel(ABC):
    """The interface every model implementation satisfies.

    Deliberately narrow: `fit`/`predict`/`predict_proba` plus enough
    metadata (`feature_columns_`, `classes_`) to validate inference-time
    input against what training-time input looked like (Sprint 10 spec,
    section 60). Nothing here reaches toward `src.risk`, `src.execution`,
    `src.portfolio`, or `src.broker` -- an architecture test
    (`tests/test_architecture.py`) enforces that no file in `src/ai`
    imports any of them.
    """

    def __init__(self, config: ModelConfig | None = None) -> None:
        self._config = config or ModelConfig()
        self._feature_columns: list[str] | None = None

    @property
    def config(self) -> ModelConfig:
        return self._config

    @property
    def is_fitted(self) -> bool:
        return self._feature_columns is not None

    @property
    def feature_columns(self) -> list[str]:
        """The exact, ordered feature columns this model was fit on.

        Raises:
            RuntimeError: called before `fit()`.
        """
        if self._feature_columns is None:
            raise RuntimeError(f"{type(self).__name__} has not been fit yet")
        return list(self._feature_columns)

    @property
    @abstractmethod
    def classes_(self) -> list[str]:
        """The class labels this model predicts among, in the order
        `predict_proba()`'s columns are returned in.

        Raises:
            RuntimeError: called before `fit()`.
        """
        raise NotImplementedError

    @abstractmethod
    def fit(self, X: pd.DataFrame, y: pd.Series) -> "AIModel":
        """Fit this model on `X`/`y`. Returns `self`.

        Callers (`src.ai.training.MLTrainingService`) must only ever
        pass the *training* split -- this method has no way to know
        whether `X` is a training, validation, or test set, and no
        opinion about it; leakage prevention is the caller's
        responsibility (`src.ai.splitting`), not this class's.
        """
        raise NotImplementedError

    @abstractmethod
    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """Predicted class label per row of `X`.

        Raises:
            RuntimeError: called before `fit()`.
            ValueError: `X`'s columns don't exactly match
                `feature_columns` (name *and* order) -- fails loudly
                rather than silently mis-aligning inputs (Sprint 10
                spec, section 60).
        """
        raise NotImplementedError

    @abstractmethod
    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        """Per-class probability for every row of `X`.

        Returns:
            A DataFrame indexed like `X`, one column per entry in
            `classes_`.

        Raises:
            RuntimeError: called before `fit()`.
            ValueError: `X`'s columns don't exactly match
                `feature_columns`.
        """
        raise NotImplementedError

    def _check_schema(self, X: pd.DataFrame) -> None:
        expected = self.feature_columns
        actual = list(X.columns)
        if actual != expected:
            raise ValueError(
                f"{type(self).__name__}: input feature schema does not match "
                f"the schema this model was trained on. Expected columns "
                f"(in order) {expected!r}, got {actual!r}. A missing feature, "
                f"a changed indicator period, or a reordered column must fail "
                f"loudly rather than silently produce a wrong prediction "
                f"(DECISIONS.md, ADR-0043)."
            )

    def to_artifact(self) -> dict:
        """This model's fitted state, in a form `src.ai.artifacts`
        can hand to `joblib.dump()` as-is and `joblib.load()` will hand
        straight back to `from_artifact()`.

        Raises:
            RuntimeError: called before `fit()`.
        """
        raise NotImplementedError

    @classmethod
    def from_artifact(cls, artifact: dict, config: ModelConfig) -> "AIModel":
        """Reconstruct a fitted model from `to_artifact()`'s output.
        Never trains -- purely restores already-fitted state."""
        raise NotImplementedError


class LogisticRegressionModel(AIModel):
    """`AIModel` backed by `sklearn.pipeline.Pipeline(StandardScaler,
    LogisticRegression)` (Sprint 10 spec, section 3).

    The scaler is fit only on whatever `fit()` is given (Sprint 10
    spec, section 6) -- there is no separate "fit scaler on everything"
    step anywhere in this class; `Pipeline.fit()` itself guarantees the
    scaler's `mean_`/`scale_` come from the same call's `X` the
    classifier trains on, and `Pipeline.predict()`/`.predict_proba()`
    only ever *transform* (never re-fit) on whatever `X` is passed
    later. `tests/test_ai_model.py`'s leakage test asserts this
    structurally, not just by inspection.
    """

    def __init__(self, config: ModelConfig | None = None) -> None:
        super().__init__(config)
        params = dict(self._config.hyperparameters)
        params.setdefault("random_state", self._config.random_state)
        params.setdefault("max_iter", 1000)
        # No explicit `multi_class=` kwarg: that parameter was deprecated
        # and later removed from scikit-learn's LogisticRegression
        # (gone as of the 1.9.0 pinned in requirements.txt) -- the
        # solver's default behavior already picks a multinomial fit for
        # a 3-class target with the default 'lbfgs' solver, which is
        # exactly what this module needs for LONG/SHORT/FLAT.
        self._pipeline = Pipeline(
            steps=[
                ("scaler", StandardScaler()),
                ("classifier", LogisticRegression(**params)),
            ]
        )

    @property
    def classes_(self) -> list[str]:
        if not self.is_fitted:
            raise RuntimeError("LogisticRegressionModel has not been fit yet")
        return list(self._pipeline.named_steps["classifier"].classes_)

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "LogisticRegressionModel":
        if len(X) == 0:
            raise ValueError("cannot fit on an empty training set")
        self._pipeline.fit(X, y)
        self._feature_columns = list(X.columns)
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        if not self.is_fitted:
            raise RuntimeError("LogisticRegressionModel has not been fit yet")
        self._check_schema(X)
        return self._pipeline.predict(X)

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        if not self.is_fitted:
            raise RuntimeError("LogisticRegressionModel has not been fit yet")
        self._check_schema(X)
        proba = self._pipeline.predict_proba(X)
        return pd.DataFrame(proba, index=X.index, columns=self.classes_)

    def to_artifact(self) -> dict:
        if not self.is_fitted:
            raise RuntimeError("LogisticRegressionModel has not been fit yet")
        return {
            "model_type": self._config.model_type,
            "pipeline": self._pipeline,
            "feature_columns": list(self._feature_columns),
            "classes": self.classes_,
        }

    @classmethod
    def from_artifact(cls, artifact: dict, config: ModelConfig) -> "LogisticRegressionModel":
        model = cls(config)
        model._pipeline = artifact["pipeline"]
        model._feature_columns = list(artifact["feature_columns"])
        return model


# ---------------------------------------------------------------------------
# Model-type registry -- the seam that keeps adding a second model
# implementation to "implement it, register it" (Sprint 10 spec,
# sections 13, 64), never a rewrite of MLTrainingService/AISignalStrategy.
# Mirrors `src.strategies.registry`'s own @register_x / lookup-by-name
# pattern (one more instance of an established idea, not a new one).
# ---------------------------------------------------------------------------

_MODEL_REGISTRY: dict[str, Callable[[ModelConfig], AIModel]] = {
    DEFAULT_MODEL_TYPE: LogisticRegressionModel,
}


def register_model_type(name: str) -> Callable[[type], type]:
    """Class decorator registering an `AIModel` implementation under
    `name`, for `build_model()`/artifact reconstruction to look up by
    string (the same way a `ModelConfig.model_type` was recorded).

    Raises:
        ValueError: `name` is already registered.
    """

    def decorator(model_cls: type) -> type:
        if name in _MODEL_REGISTRY:
            raise ValueError(f"a model type is already registered under {name!r}")
        _MODEL_REGISTRY[name] = model_cls
        return model_cls

    return decorator


def build_model(config: ModelConfig) -> AIModel:
    """Construct an unfitted `AIModel` for `config.model_type`.

    Raises:
        KeyError: `config.model_type` isn't registered.
    """
    try:
        model_cls = _MODEL_REGISTRY[config.model_type]
    except KeyError as exc:
        raise KeyError(
            f"no model type registered under {config.model_type!r} -- "
            f"available: {sorted(_MODEL_REGISTRY)}"
        ) from exc
    return model_cls(config)


def model_class_for(model_type: str) -> type:
    """The `AIModel` subclass registered under `model_type` -- used by
    `src.ai.artifacts`/`src.ai.registry` to call `from_artifact()` on
    the right class when reloading a saved model.

    Raises:
        KeyError: `model_type` isn't registered.
    """
    try:
        return _MODEL_REGISTRY[model_type]
    except KeyError as exc:
        raise KeyError(
            f"no model type registered under {model_type!r} -- "
            f"available: {sorted(_MODEL_REGISTRY)}"
        ) from exc
