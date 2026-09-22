"""AI Signal Strategy -- src/strategies/ai_signal.py.

The one place a trained `src.ai` model actually becomes a `Strategy`
(Sprint 10 spec, sections 24-30, `DECISIONS.md` ADR-0043). Everything
upstream of this file (features, labels, splitting, training,
evaluation, artifact persistence) has no idea a `Signal` or a
`Backtester` exists; everything downstream of it (`Backtester`,
`src.risk`, `src.execution`, `src.portfolio`) has no idea a model
exists at all. This adapter is the seam between the two, and nothing
more:

    features -> model inference -> predicted class/probability -> Signal

`AISignalStrategy` loads an already-trained, frozen model by
`model_id` at construction time (`src.ai.registry.ModelRegistry.
load_model()`) and never trains or refits it -- consistent with every
other strategy in this codebase treating its own decision logic as
fixed for the duration of a backtest, and required explicitly by
Sprint 10 spec section 24 ("it must not train the model itself").

    from src.strategies.ai_signal import AISignalStrategy

    strategy = AISignalStrategy(symbol="SPY", model_id=result.model_id, min_probability=0.55)
    result = Backtester().run(strategy, candles)

Satisfies `Strategy` (`src/strategies/base.py`) via `BaseStrategy`
(`src/strategies/sdk.py`) like every other strategy -- `Backtester`
needs no AI-specific branch to run it (Sprint 10 spec, section 66).
"""

from __future__ import annotations

import pandas as pd

from src.ai.features import FeatureBuilder, FeatureSpec
from src.ai.registry import ModelRegistry
from src.signals.models import Signal, SignalDirection
from src.strategies.registry import register_strategy
from src.strategies.sdk import BaseStrategy

DEFAULT_MIN_PROBABILITY = 0.55


@register_strategy("ai_signal")
class AISignalStrategy(BaseStrategy):
    """Long/short/flat based on a trained classifier's own predicted
    class and confidence, thresholded by `min_probability`.

    Args:
        symbol: which instrument this strategy instance decides for
            (`BaseStrategy`, `DECISIONS.md` ADR-0033).
        model_id: the trained model to load
            (`src.ai.identity.model_spec_id`'s output, as returned by
            `src.ai.training.MLTrainingService.train()`).
        min_probability: below this, a prediction is treated as
            `FLAT`/no-position-change regardless of which class the
            model actually favored (Sprint 10 spec, section 27) --
            probability is never translated directly into position
            size (section 59); that stays `src.risk`'s job entirely.
        registry: where to load `model_id` from. Defaults to
            `ModelRegistry()` at the platform's default model
            directory -- deliberately not overridable via a mutable
            module-level global, so `ExperimentSpec.reconstruct_strategy()`
            (`src.experiments.spec`) rebuilds the exact same strategy
            from `model_id`/`min_probability` alone, every time
            (Sprint 10 spec, section 30).

    Raises:
        ValueError: `min_probability` isn't in `(0.0, 1.0]`.
        KeyError: `model_id` isn't a registered model.
        FileNotFoundError: `model_id`'s metadata exists but its
            artifact file is missing.
    """

    def __init__(
        self,
        symbol: str,
        model_id: str,
        min_probability: float = DEFAULT_MIN_PROBABILITY,
        registry: ModelRegistry | None = None,
    ) -> None:
        if not 0.0 < min_probability <= 1.0:
            raise ValueError(
                f"min_probability must be in (0.0, 1.0], got {min_probability}"
            )
        super().__init__(name="ai_signal", symbol=symbol)
        self._model_id = model_id
        self._min_probability = min_probability
        self._registry = registry or ModelRegistry()
        # Loaded once, at construction -- frozen for the lifetime of
        # this strategy instance. Nothing below ever calls .fit() again.
        self._model, self._metadata = self._registry.load_model(model_id)
        feature_spec_kwargs = dict(self._metadata.feature_spec)
        feature_spec_kwargs["return_periods"] = tuple(feature_spec_kwargs["return_periods"])
        self._feature_builder = FeatureBuilder(FeatureSpec(**feature_spec_kwargs))

    @property
    def params(self) -> dict:
        """`model_id`/`min_probability` -- everything
        `ExperimentSpec.reconstruct_strategy()` needs to rebuild an
        equivalent instance (Sprint 10 spec, section 30). Deliberately
        does not include the model's own hyperparameters/feature
        config/dataset fingerprint -- those live in `ModelRegistry`,
        keyed by `model_id`, and are the model's provenance, not this
        strategy instance's own constructor arguments."""
        return {"model_id": self._model_id, "min_probability": self._min_probability}

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Enrich `data` with this model's exact feature columns.

        Raises:
            ValueError: `data` is missing a required OHLCV column, or
                the features actually computed from `data` don't
                exactly match (name *and* order) what `model_id` was
                trained on -- fails loudly rather than silently running
                inference against a mismatched schema (Sprint 10 spec,
                sections 32, 60).
        """
        self.require_columns(data)
        features = self._feature_builder.build(data)
        actual_columns = list(features.columns)
        expected_columns = list(self._metadata.feature_columns)
        if actual_columns != expected_columns:
            raise ValueError(
                f"ai_signal: computed feature columns {actual_columns!r} do not "
                f"match model {self._model_id!r}'s expected feature_columns "
                f"{expected_columns!r} -- refusing to run inference with a "
                f"mismatched feature schema (DECISIONS.md, ADR-0043)."
            )
        out = data.copy()
        for column in features.columns:
            out[column] = features[column]
        return out

    def generate_signals(self, data: pd.DataFrame) -> list[Signal]:
        """Sparse signals from this model's predictions (Sprint 10
        spec, section 28, `DECISIONS.md` ADR-0015) -- emits a `Signal`
        only when the *effective* direction (post-`min_probability`
        threshold) changes, not on every row the model has an opinion
        about. Does not retrain or refit the model in any way; purely
        reads `predict_proba()`.
        """
        feature_columns = list(self._metadata.feature_columns)
        valid_mask = data[feature_columns].notna().all(axis=1)
        valid_data = data.loc[valid_mask, feature_columns]

        signals: list[Signal] = []
        if valid_data.empty:
            return signals

        proba = self._model.predict_proba(valid_data)

        state = SignalDirection.FLAT
        for timestamp in data.index:
            if timestamp not in proba.index:
                continue  # feature warmup -- no opinion yet

            row = proba.loc[timestamp]
            predicted_class = str(row.idxmax())
            class_probability = float(row.max())
            meets_threshold = class_probability >= self._min_probability
            effective_direction = (
                SignalDirection(predicted_class) if meets_threshold else SignalDirection.FLAT
            )

            if effective_direction == state:
                continue

            threshold_note = (
                f"meets min_probability {self._min_probability}"
                if meets_threshold
                else f"below min_probability {self._min_probability} -- held FLAT"
            )
            signals.append(
                self.emit_signal(
                    timestamp,
                    effective_direction,
                    confidence=class_probability,
                    reason=(
                        f"model {self._model_id[:12]} predicted {predicted_class} "
                        f"(p={class_probability:.3f}); {threshold_note}"
                    ),
                    model_id=self._model_id,
                    model_artifact_hash=self._metadata.artifact_hash,
                    predicted_class=predicted_class,
                    class_probability=class_probability,
                    feature_set_id=self._metadata.feature_set_id,
                    label_horizon_bars=self._metadata.label_spec.get("horizon_bars"),
                )
            )
            state = effective_direction

        return signals
