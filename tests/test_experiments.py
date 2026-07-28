"""Tests for the Experiment Registry (src/experiments).

Uses a tmp_path-backed SQLite file per test so tests never touch the
real `data/experiments.db` and never interfere with each other.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.experiments.registry import ExperimentRegistry
from src.signals.models import Signal, SignalDirection


def make_registry(tmp_path) -> ExperimentRegistry:
    return ExperimentRegistry(db_path=tmp_path / "experiments.db")


def make_signal(**overrides) -> Signal:
    defaults = dict(
        timestamp=pd.Timestamp("2024-01-01"),
        direction=SignalDirection.LONG,
        confidence=0.8,
        metadata={"reason": "test"},
    )
    defaults.update(overrides)
    return Signal(**defaults)


def test_log_experiment_returns_incrementing_ids(tmp_path):
    registry = make_registry(tmp_path)

    first_id = registry.log_experiment(
        changed={"RSI.period": [14, 10]},
        metrics_before={"sharpe": 1.31},
        metrics_after={"sharpe": 1.42},
        decision="KEEP",
    )
    second_id = registry.log_experiment(
        changed={"MACD.fast": [12, 8]},
        metrics_before={"sharpe": 1.42},
        metrics_after={"sharpe": 1.38},
        decision="DISCARD",
    )

    assert second_id == first_id + 1


def test_get_experiment_round_trips_all_fields(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = registry.log_experiment(
        changed={"RSI.period": [14, 10]},
        metrics_before={"sharpe": 1.31, "win_rate": 0.56},
        metrics_after={"sharpe": 1.42, "win_rate": 0.59},
        decision="keep",  # lowercase on the way in
        strategy_name="sma_cross",
        notes="promising",
    )

    experiment = registry.get_experiment(experiment_id)

    assert experiment is not None
    assert experiment.id == experiment_id
    assert experiment.strategy_name == "sma_cross"
    assert experiment.changed == {"RSI.period": [14, 10]}
    assert experiment.metrics_before == {"sharpe": 1.31, "win_rate": 0.56}
    assert experiment.metrics_after == {"sharpe": 1.42, "win_rate": 0.59}
    assert experiment.decision == "KEEP"  # normalized to uppercase
    assert experiment.notes == "promising"
    assert experiment.created_at  # non-empty timestamp


def test_get_experiment_returns_none_for_missing_id(tmp_path):
    registry = make_registry(tmp_path)
    assert registry.get_experiment(999) is None


def test_invalid_decision_raises_value_error(tmp_path):
    registry = make_registry(tmp_path)

    with pytest.raises(ValueError):
        registry.log_experiment(
            changed={},
            metrics_before={},
            metrics_after={},
            decision="MAYBE",
        )


def test_list_experiments_returns_all_in_order(tmp_path):
    registry = make_registry(tmp_path)
    ids = [
        registry.log_experiment(
            changed={"x": [i, i + 1]},
            metrics_before={},
            metrics_after={},
            decision="KEEP",
        )
        for i in range(3)
    ]

    experiments = registry.list_experiments()

    assert [e.id for e in experiments] == ids


def test_list_experiments_filters_by_decision(tmp_path):
    registry = make_registry(tmp_path)
    registry.log_experiment(changed={}, metrics_before={}, metrics_after={}, decision="KEEP")
    registry.log_experiment(changed={}, metrics_before={}, metrics_after={}, decision="DISCARD")
    registry.log_experiment(changed={}, metrics_before={}, metrics_after={}, decision="KEEP")

    kept = registry.list_experiments(decision="KEEP")

    assert len(kept) == 2
    assert all(e.decision == "KEEP" for e in kept)


def test_list_experiments_filters_by_strategy_name(tmp_path):
    registry = make_registry(tmp_path)
    registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP",
        strategy_name="sma_cross",
    )
    registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP",
        strategy_name="rsi_reversion",
    )

    sma_only = registry.list_experiments(strategy_name="sma_cross")

    assert len(sma_only) == 1
    assert sma_only[0].strategy_name == "sma_cross"


def test_count_reflects_number_of_experiments(tmp_path):
    registry = make_registry(tmp_path)
    assert registry.count() == 0

    registry.log_experiment(changed={}, metrics_before={}, metrics_after={}, decision="KEEP")
    registry.log_experiment(changed={}, metrics_before={}, metrics_after={}, decision="KEEP")

    assert registry.count() == 2


def test_registry_persists_across_reconnects(tmp_path):
    db_path = tmp_path / "experiments.db"
    first_registry = ExperimentRegistry(db_path=db_path)
    experiment_id = first_registry.log_experiment(
        changed={"RSI.period": [14, 10]},
        metrics_before={"sharpe": 1.31},
        metrics_after={"sharpe": 1.42},
        decision="KEEP",
    )

    second_registry = ExperimentRegistry(db_path=db_path)
    experiment = second_registry.get_experiment(experiment_id)

    assert experiment is not None
    assert experiment.changed == {"RSI.period": [14, 10]}


def test_summary_includes_key_fields(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = registry.log_experiment(
        changed={"RSI.period": [14, 10]},
        metrics_before={"sharpe": 1.31, "win_rate": 0.56},
        metrics_after={"sharpe": 1.42, "win_rate": 0.59},
        decision="KEEP",
        strategy_name="sma_cross",
    )

    summary = registry.get_experiment(experiment_id).summary()

    assert f"Experiment #{experiment_id}" in summary
    assert "sma_cross" in summary
    assert "14 -> 10" in summary
    assert "KEEP" in summary


# ---------------------------------------------------------------------------
# Signal storage (DECISIONS.md, ADR-0016) -- signals are first-class rows
# here, not a separate repository.
# ---------------------------------------------------------------------------

def test_save_and_get_signals_round_trips(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )
    signal = make_signal(metadata={"trend": "UP", "reason": "EMA20 crossed EMA50"})

    registry.save_signals(experiment_id, [signal])
    fetched = registry.get_signals(experiment_id)

    assert len(fetched) == 1
    assert fetched[0].id == signal.id
    assert fetched[0].timestamp == signal.timestamp
    assert fetched[0].direction == signal.direction
    assert fetched[0].confidence == signal.confidence
    assert fetched[0].metadata == {"trend": "UP", "reason": "EMA20 crossed EMA50"}


def test_get_signals_orders_by_timestamp(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )
    later = make_signal(timestamp=pd.Timestamp("2024-01-03"))
    earlier = make_signal(timestamp=pd.Timestamp("2024-01-01"))

    registry.save_signals(experiment_id, [later, earlier])
    fetched = registry.get_signals(experiment_id)

    assert [s.id for s in fetched] == [earlier.id, later.id]


def test_get_signals_only_returns_signals_for_that_experiment(tmp_path):
    registry = make_registry(tmp_path)
    experiment_a = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )
    experiment_b = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="DISCARD"
    )
    signal_a = make_signal()
    signal_b = make_signal()

    registry.save_signals(experiment_a, [signal_a])
    registry.save_signals(experiment_b, [signal_b])

    assert [s.id for s in registry.get_signals(experiment_a)] == [signal_a.id]
    assert [s.id for s in registry.get_signals(experiment_b)] == [signal_b.id]


def test_get_signal_fetches_by_id_directly(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )
    signal = make_signal()
    registry.save_signals(experiment_id, [signal])

    fetched = registry.get_signal(signal.id)

    assert fetched is not None
    assert fetched.id == signal.id


def test_get_signal_returns_none_for_missing_id(tmp_path):
    registry = make_registry(tmp_path)
    assert registry.get_signal(make_signal().id) is None


def test_signals_persist_across_reconnects(tmp_path):
    db_path = tmp_path / "experiments.db"
    first_registry = ExperimentRegistry(db_path=db_path)
    experiment_id = first_registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )
    signal = make_signal()
    first_registry.save_signals(experiment_id, [signal])

    second_registry = ExperimentRegistry(db_path=db_path)
    fetched = second_registry.get_signal(signal.id)

    assert fetched is not None
    assert fetched.metadata == signal.metadata
