"""Tests for the Experiment Registry (src/experiments).

Uses a tmp_path-backed SQLite file per test so tests never touch the
real `data/experiments.db` and never interfere with each other.
"""

from __future__ import annotations

import pytest

from src.experiments.registry import ExperimentRegistry


def make_registry(tmp_path) -> ExperimentRegistry:
    return ExperimentRegistry(db_path=tmp_path / "experiments.db")


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
