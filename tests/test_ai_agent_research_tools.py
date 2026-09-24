"""Tests for the six read-only research tools -- Sprint 13 spec,
sections 17-22, 75. Every tool must use an existing platform application
service, never duplicate a calculation, and never leak a filesystem
path/secret. No network access.
"""

from __future__ import annotations

import pandas as pd
import pytest

pytest.importorskip("joblib")  # transitively required by src.ai.registry.ModelRegistry

from src.ai.agents.policy import ResearchAgentPolicy
from src.ai.agents.research_tools import (
    AnalyzeExperimentTool,
    CompareExperimentsTool,
    GetExperimentTool,
    GetModelMetadataTool,
    ListExperimentsTool,
    ListStrategiesTool,
)
from src.ai.registry import ModelMetadata, ModelRegistry
from src.backtesting.models import Trade
from src.experiments.registry import ExperimentRegistry
from src.experiments.spec import ExperimentSpec
from src.risk.models import RiskLimits
from src.strategies.ema_cross import EMACrossStrategy  # noqa: F401 -- registers "ema_cross"
from src.strategies.rsi_mean_reversion import RSIMeanReversionStrategy  # noqa: F401


@pytest.fixture
def registry(tmp_path) -> ExperimentRegistry:
    return ExperimentRegistry(db_path=tmp_path / "experiments.db")


def _candles(n: int = 30) -> pd.DataFrame:
    closes = [100.0 + i for i in range(n)]
    dates = pd.date_range("2024-01-01", periods=n, freq="D", name="timestamp", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
         "close": closes, "volume": [1000.0] * n},
        index=dates,
    )


def _seed_experiment(registry: ExperimentRegistry, *, symbol="SPY", decision="KEEP") -> int:
    experiment_id = registry.log_experiment(
        changed={"fast": [10, 12]}, metrics_before={"sharpe": 1.0}, metrics_after={"sharpe": 1.2},
        decision=decision, strategy_name="ema_cross",
    )
    strategy = EMACrossStrategy(symbol=symbol)
    candles = _candles()
    spec = ExperimentSpec.capture(
        strategy, candles, RiskLimits(), symbol=symbol, interval="1d", dataset_source="synthetic",
    )
    registry.save_spec(experiment_id, spec)
    trades = [
        Trade(
            entry_time=candles.index[0], exit_time=candles.index[5], direction=1,
            entry_price=100.0, exit_price=110.0, entry_signal_id=__import__("uuid").uuid4(),
        )
    ]
    registry.save_trades(experiment_id, trades)
    registry.save_equity_curve(experiment_id, pd.Series([100_000.0, 101_000.0], index=candles.index[:2]))
    return experiment_id


POLICY = ResearchAgentPolicy()


# ---------------------------------------------------------------------------
# list_experiments
# ---------------------------------------------------------------------------


def test_list_experiments_returns_compact_rows(registry):
    _seed_experiment(registry, symbol="SPY")
    _seed_experiment(registry, symbol="QQQ")
    tool = ListExperimentsTool(registry)
    result = tool.execute({})
    assert result.status == "ok"
    assert result.data["count"] == 2
    row = result.data["experiments"][0]
    assert set(row) == {"experiment_id", "created_at", "strategy_name", "decision", "symbol", "interval"}


def test_list_experiments_filters_by_symbol(registry):
    _seed_experiment(registry, symbol="SPY")
    _seed_experiment(registry, symbol="QQQ")
    tool = ListExperimentsTool(registry)
    result = tool.execute({"symbol": "QQQ"})
    assert result.data["count"] == 1
    assert result.data["experiments"][0]["symbol"] == "QQQ"


def test_list_experiments_respects_limit(registry):
    for _ in range(5):
        _seed_experiment(registry)
    tool = ListExperimentsTool(registry)
    result = tool.execute({"limit": 2})
    assert result.data["count"] == 2


def test_list_experiments_empty_registry_returns_empty_list(registry):
    tool = ListExperimentsTool(registry)
    result = tool.execute({})
    assert result.data == {"experiments": [], "count": 0}


# ---------------------------------------------------------------------------
# get_experiment
# ---------------------------------------------------------------------------


def test_get_experiment_returns_full_compact_package(registry):
    experiment_id = _seed_experiment(registry)
    tool = GetExperimentTool(registry)
    result = tool.execute({"experiment_id": experiment_id})
    assert result.status == "ok"
    assert result.data["experiment_id"] == experiment_id
    assert result.data["spec"]["symbol"] == "SPY"
    assert result.data["analytics"]["trade_count"] == 1
    assert result.data["trade_count"] == 1


def test_get_experiment_not_found_returns_structured_error(registry):
    tool = GetExperimentTool(registry)
    result = tool.execute({"experiment_id": 9999})
    assert result.is_error
    assert result.error_code == "NOT_FOUND"


def test_get_experiment_never_dumps_full_trade_list(registry):
    experiment_id = _seed_experiment(registry)
    tool = GetExperimentTool(registry)
    result = tool.execute({"experiment_id": experiment_id})
    assert "trades" not in result.data
    assert "equity_curve" not in result.data


# ---------------------------------------------------------------------------
# analyze_experiment
# ---------------------------------------------------------------------------


def test_analyze_experiment_delegates_to_analytics_service(registry):
    experiment_id = _seed_experiment(registry)
    tool = AnalyzeExperimentTool(registry)
    result = tool.execute({"experiment_id": experiment_id})
    assert result.status == "ok"
    assert result.data["trade_count"] == 1
    assert "sharpe_ratio" in result.data
    assert set(result.data["sharpe_ratio"]) == {"value", "status", "reason"}


def test_analyze_experiment_not_found(registry):
    tool = AnalyzeExperimentTool(registry)
    result = tool.execute({"experiment_id": 12345})
    assert result.is_error
    assert result.error_code == "NOT_FOUND"


def test_analyze_experiment_undefined_metrics_stay_explicit(registry):
    # An experiment with no trades/equity curve saved at all -- every
    # metric that needs them must come back UNDEFINED, never a
    # fabricated number (mirrors AnalyticsService's own contract).
    experiment_id = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="INCONCLUSIVE",
    )
    tool = AnalyzeExperimentTool(registry)
    result = tool.execute({"experiment_id": experiment_id})
    assert result.data["sharpe_ratio"]["status"] == "undefined"
    assert result.data["sharpe_ratio"]["value"] is None


# ---------------------------------------------------------------------------
# compare_experiments
# ---------------------------------------------------------------------------


def test_compare_experiments_returns_rows_and_warnings_no_ranking(registry):
    id_a = _seed_experiment(registry, symbol="SPY")
    id_b = _seed_experiment(registry, symbol="QQQ")
    tool = CompareExperimentsTool(registry)
    result = tool.execute({"experiment_ids": [id_a, id_b]})
    assert result.status == "ok"
    assert len(result.data["rows"]) == 2
    assert any(w["field"] == "symbol" for w in result.data["warnings"])
    assert "best_strategy" not in result.data
    assert "ranking" not in result.data


def test_compare_experiments_requires_at_least_two_ids(registry):
    experiment_id = _seed_experiment(registry)
    tool = CompareExperimentsTool(registry)
    result = tool.execute({"experiment_ids": [experiment_id]})
    assert result.is_error
    assert result.error_code == "INVALID_ARGUMENTS"


def test_compare_experiments_missing_id_is_structured_error(registry):
    experiment_id = _seed_experiment(registry)
    tool = CompareExperimentsTool(registry)
    result = tool.execute({"experiment_ids": [experiment_id, 999999]})
    assert result.is_error
    assert result.error_code == "NOT_FOUND"


# ---------------------------------------------------------------------------
# list_strategies
# ---------------------------------------------------------------------------


def test_list_strategies_includes_registered_strategies_without_source_code():
    tool = ListStrategiesTool()
    result = tool.execute({})
    assert result.status == "ok"
    names = {row["strategy_name"] for row in result.data["strategies"]}
    assert "ema_cross" in names
    assert "rsi_mean_reversion" in names
    for row in result.data["strategies"]:
        assert "source" not in row
        assert "code" not in row
    ema_row = next(r for r in result.data["strategies"] if r["strategy_name"] == "ema_cross")
    param_names = {p["name"] for p in ema_row["parameters"]}
    assert {"fast", "slow", "confidence"}.issubset(param_names)


# ---------------------------------------------------------------------------
# get_model_metadata
# ---------------------------------------------------------------------------


@pytest.fixture
def model_registry(tmp_path) -> ModelRegistry:
    return ModelRegistry(base_dir=tmp_path / "models")


def _seed_model_metadata(registry: ModelRegistry, model_id: str = "model-abc") -> None:
    metadata = ModelMetadata(
        model_id=model_id, model_type="logistic_regression", hyperparameters={"C": 1.0},
        random_state=42, feature_spec={"return_periods": [1, 5]}, feature_set_id="fs1",
        feature_columns=["ret_1", "ret_5"], label_spec={"horizon_bars": 5}, label_spec_id="ls1",
        classes=["LONG", "SHORT", "FLAT"], dataset_fingerprint="fp1", dataset_source="synthetic",
        symbol="SPY", interval="1d", train_start="2020-01-01", train_end="2021-01-01",
        validation_start=None, validation_end=None, test_start=None, test_end=None,
        artifact_hash="hash-xyz",
    )
    registry.save_metadata(metadata)


def test_get_model_metadata_returns_provenance_without_filesystem_paths(model_registry):
    _seed_model_metadata(model_registry)
    tool = GetModelMetadataTool(model_registry)
    result = tool.execute({"model_id": "model-abc"})
    assert result.status == "ok"
    assert result.data["model_id"] == "model-abc"
    assert result.data["artifact_hash"] == "hash-xyz"
    for value in result.data.values():
        assert not (isinstance(value, str) and value.startswith("/"))


def test_get_model_metadata_not_found(model_registry):
    tool = GetModelMetadataTool(model_registry)
    result = tool.execute({"model_id": "does-not-exist"})
    assert result.is_error
    assert result.error_code == "NOT_FOUND"
