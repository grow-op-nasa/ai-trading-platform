"""Tests for `src/strategies/ai_signal.py` (Sprint 10, `DECISIONS.md`
ADR-0043).

Gated on scikit-learn/joblib (see `tests/test_ai_model.py`'s module
docstring).
"""

from __future__ import annotations

import dataclasses
import random

import pandas as pd
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("joblib")

from src.ai.artifacts import ModelArtifactStore
from src.ai.labels import LabelSpec
from src.ai.registry import ModelRegistry
from src.ai.training import MLTrainingService
from src.backtesting.engine import Backtester
from src.data.base import Interval
from src.data.canonical import canonicalize_candles
from src.data.models import CandleDataset, SessionPolicy, ValidationReport
from src.experiments.spec import ExperimentSpec
from src.risk.models import RiskLimits
from src.signals.models import SignalDirection
from src.strategies.ai_signal import AISignalStrategy
from src.strategies.registry import get_strategy_class
from src.utils.hashing import dataframe_fingerprint


def _make_candles(n: int = 260, seed: int = 21) -> pd.DataFrame:
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


def _make_dataset(candles: pd.DataFrame, symbol: str = "SPY") -> CandleDataset:
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


def _train_model(tmp_path, **train_kwargs):
    candles = _make_candles()
    dataset = _make_dataset(candles)
    store = ModelArtifactStore(tmp_path)
    registry = ModelRegistry(tmp_path, artifact_store=store)
    service = MLTrainingService(registry=registry, artifact_store=store)
    result = service.train(dataset, **train_kwargs)
    return result, registry, candles


def test_strategy_is_registered_under_ai_signal():
    assert get_strategy_class("ai_signal") is AISignalStrategy


def test_strategy_produces_signals_with_correct_symbol_and_bounded_confidence(tmp_path):
    result, registry, candles = _train_model(tmp_path)
    strategy = AISignalStrategy(
        symbol="SPY", model_id=result.model_id, min_probability=0.4, registry=registry
    )

    backtest = Backtester().run(strategy, candles)

    assert backtest.signals, "expected at least one signal for a low probability threshold"
    for signal in backtest.signals:
        assert signal.symbol == "SPY"
        assert isinstance(signal.direction, SignalDirection)
        assert 0.0 <= signal.confidence <= 1.0
        assert signal.metadata["model_id"] == result.model_id


def test_high_min_probability_yields_no_signals_when_never_confident_enough(tmp_path):
    result, registry, candles = _train_model(tmp_path)
    # A single-feature-set logistic regression essentially never reaches
    # 99.9% confidence on noisy synthetic data -- the threshold should
    # hold the strategy at its FLAT starting state throughout.
    strategy = AISignalStrategy(
        symbol="SPY", model_id=result.model_id, min_probability=0.999, registry=registry
    )
    backtest = Backtester().run(strategy, candles)
    assert backtest.signals == []


def test_signals_are_sparse_not_one_per_valid_row(tmp_path):
    result, registry, candles = _train_model(tmp_path)
    strategy = AISignalStrategy(
        symbol="SPY", model_id=result.model_id, min_probability=0.4, registry=registry
    )
    backtest = Backtester().run(strategy, candles)
    # Far fewer signals than candles -- sparse-signal semantics
    # (DECISIONS.md, ADR-0015), not a dense per-row emission.
    assert len(backtest.signals) < len(candles) // 2


def test_strategy_never_retrains_the_model(tmp_path):
    result, registry, candles = _train_model(tmp_path)
    strategy = AISignalStrategy(
        symbol="SPY", model_id=result.model_id, min_probability=0.4, registry=registry
    )
    prepared = strategy.prepare(candles)
    before = strategy._model.predict_proba(
        prepared[result.feature_columns].dropna()
    )
    strategy.generate_signals(prepared)
    after = strategy._model.predict_proba(prepared[result.feature_columns].dropna())
    pd.testing.assert_frame_equal(before, after)


def test_params_are_enough_to_reconstruct_an_equivalent_strategy(tmp_path):
    result, registry, candles = _train_model(tmp_path)
    original = AISignalStrategy(
        symbol="SPY", model_id=result.model_id, min_probability=0.4, registry=registry
    )
    spec = ExperimentSpec.capture(
        original,
        candles,
        RiskLimits(),
        symbol="SPY",
        interval="1d",
        dataset_source="synthetic",
    )
    assert spec.strategy_name == "ai_signal"
    assert spec.strategy_params == {"model_id": result.model_id, "min_probability": 0.4}

    strategy_cls = get_strategy_class(spec.strategy_name)
    reconstructed = strategy_cls(symbol=spec.symbol, registry=registry, **spec.strategy_params)

    backtest_a = Backtester().run(original, candles)
    backtest_b = Backtester().run(reconstructed, candles)
    assert [s.direction for s in backtest_a.signals] == [s.direction for s in backtest_b.signals]


def test_invalid_min_probability_raises(tmp_path):
    result, registry, _ = _train_model(tmp_path)
    with pytest.raises(ValueError):
        AISignalStrategy(symbol="SPY", model_id=result.model_id, min_probability=0.0, registry=registry)
    with pytest.raises(ValueError):
        AISignalStrategy(symbol="SPY", model_id=result.model_id, min_probability=1.5, registry=registry)


def test_unknown_model_id_raises_clearly(tmp_path):
    registry = ModelRegistry(tmp_path)
    with pytest.raises(KeyError):
        AISignalStrategy(symbol="SPY", model_id="not-a-real-model", registry=registry)


def test_mismatched_feature_schema_fails_loudly_in_prepare(tmp_path):
    result, registry, candles = _train_model(tmp_path)
    # Corrupt the saved metadata's feature_columns to simulate a model
    # trained against a different feature configuration than what's
    # actually being computed now (Sprint 10 spec, sections 32, 60).
    metadata = registry.get_metadata(result.model_id)
    tampered = dataclasses.replace(
        metadata, feature_columns=["not", "the", "real", "columns"]
    )
    registry.save_metadata(tampered)

    strategy = AISignalStrategy(symbol="SPY", model_id=result.model_id, registry=registry)
    with pytest.raises(ValueError, match="feature columns"):
        strategy.prepare(candles)


def test_different_label_horizon_flows_into_signal_metadata(tmp_path):
    result, registry, candles = _train_model(tmp_path, label_spec=LabelSpec(horizon_bars=10))
    strategy = AISignalStrategy(
        symbol="SPY", model_id=result.model_id, min_probability=0.4, registry=registry
    )
    backtest = Backtester().run(strategy, candles)
    assert backtest.signals
    assert backtest.signals[0].metadata["label_horizon_bars"] == 10
