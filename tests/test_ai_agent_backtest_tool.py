"""Tests for the `run_historical_backtest` agent tool -- Sprint 13
spec, sections 23-29, 76 (Phase 7): input validation, historical-only
enforcement, bounded date range, no filesystem/URL/shell/broker inputs,
and that it actually routes through MarketDataService ->
PortfolioBacktestEngine -> Analytics via `ResearchTrialService`, never a
second backtest pipeline.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

pytest.importorskip("joblib")  # transitively required by src.ai.registry.ModelRegistry

from src.ai.agents.backtest_tool import MAX_DATE_RANGE_DAYS, RunHistoricalBacktestTool
from src.ai.agents.tools import validate_arguments
from src.data.base import Interval
from src.data.models import CandleDataset, SessionPolicy, ValidationReport
from src.research.trial_service import ResearchTrialService
from src.strategies.ema_cross import EMACrossStrategy  # noqa: F401


def _candles(n: int = 60) -> pd.DataFrame:
    closes = [100.0 + (i % 7) - 3 + i * 0.1 for i in range(n)]
    dates = pd.date_range("2024-01-01", periods=n, freq="D", name="timestamp", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
         "close": closes, "volume": [1000.0] * n},
        index=dates,
    )


class FakeMarketDataService:
    def __init__(self, candles=None) -> None:
        self._candles = candles if candles is not None else _candles()

    def get_dataset(self, symbol, start=None, end=None, interval=Interval.DAY_1, **kwargs):
        interval = Interval(interval)
        return CandleDataset(
            symbol=symbol, interval=interval, provider="fake", candles=self._candles,
            content_hash="fake-hash", validation_report=ValidationReport(symbol=symbol, interval=interval),
            session_policy=SessionPolicy.REGULAR, session_timezone="America/New_York",
            requested_start=start or date(2024, 1, 1), requested_end=end or date(2024, 3, 1),
            dataset_start=self._candles.index.min(), dataset_end=self._candles.index.max(),
        )


@pytest.fixture
def tool() -> RunHistoricalBacktestTool:
    service = ResearchTrialService(market_data_service=FakeMarketDataService())
    return RunHistoricalBacktestTool(service)


VALID_ARGS = {
    "strategy_name": "ema_cross",
    "strategy_params": {"fast": 3, "slow": 8},
    "symbol": "SPY",
    "interval": "1d",
    "start": "2024-01-01",
    "end": "2024-03-01",
}


def test_valid_call_returns_compact_provenance_and_analytics(tool):
    result = tool.execute(VALID_ARGS)
    assert result.status == "ok"
    assert result.data["trial_id"]
    assert result.data["symbol"] == "SPY"
    assert result.data["strategy_name"] == "ema_cross"
    assert "analytics" in result.data
    assert "sharpe_ratio" in result.data["analytics"]
    # Never a raw trade list or equity curve.
    assert "trades" not in result.data
    assert "equity_curve" not in result.data


def test_schema_rejects_missing_required_fields():
    schema = RunHistoricalBacktestTool(None).schema()
    error = validate_arguments(schema, {"strategy_name": "ema_cross"})
    assert error is not None


def test_future_end_date_is_rejected(tool):
    args = dict(VALID_ARGS, end=(date.today() + timedelta(days=10)).isoformat())
    result = tool.execute(args)
    assert result.is_error
    assert result.error_code == "HISTORICAL_ONLY"


def test_start_after_end_is_rejected(tool):
    args = dict(VALID_ARGS, start="2024-06-01", end="2024-01-01")
    result = tool.execute(args)
    assert result.is_error
    assert result.error_code == "INVALID_DATE_RANGE"


def test_malformed_date_is_rejected(tool):
    args = dict(VALID_ARGS, start="not-a-date")
    result = tool.execute(args)
    assert result.is_error
    assert result.error_code == "INVALID_DATE_RANGE"


def test_date_range_exceeding_maximum_is_rejected(tool):
    start = date(2000, 1, 1)
    end = start + timedelta(days=MAX_DATE_RANGE_DAYS + 30)
    args = dict(VALID_ARGS, start=start.isoformat(), end=min(end, date.today()).isoformat())
    if (date.fromisoformat(args["end"]) - start).days <= MAX_DATE_RANGE_DAYS:
        pytest.skip("today() too close to start to exceed MAX_DATE_RANGE_DAYS in this environment")
    result = tool.execute(args)
    assert result.is_error
    assert result.error_code == "INVALID_DATE_RANGE"


def test_invalid_symbol_is_rejected(tool):
    args = dict(VALID_ARGS, symbol="../../etc/passwd")
    result = tool.execute(args)
    assert result.is_error
    assert result.error_code == "INVALID_ARGUMENTS"


def test_unknown_strategy_is_rejected(tool):
    args = dict(VALID_ARGS, strategy_name="not_a_real_strategy")
    result = tool.execute(args)
    assert result.is_error
    assert result.error_code == "UNKNOWN_STRATEGY"


def test_too_many_strategy_params_is_rejected(tool):
    args = dict(VALID_ARGS, strategy_params={f"p{i}": i for i in range(20)})
    result = tool.execute(args)
    assert result.is_error
    assert result.error_code == "INVALID_ARGUMENTS"


def test_non_primitive_strategy_param_value_is_rejected(tool):
    args = dict(VALID_ARGS, strategy_params={"fast": {"nested": "object"}})
    result = tool.execute(args)
    assert result.is_error
    assert result.error_code == "INVALID_ARGUMENTS"


def test_schema_has_no_filesystem_url_or_shell_style_fields():
    schema = RunHistoricalBacktestTool(None).schema()
    property_names = set(schema["properties"])
    forbidden = {"path", "file", "url", "command", "shell", "broker", "api_key", "credentials"}
    assert property_names.isdisjoint(forbidden)


def test_schema_rejects_unexpected_field_like_a_broker_name():
    schema = RunHistoricalBacktestTool(None).schema()
    error = validate_arguments(schema, dict(VALID_ARGS, broker="alpaca"))
    assert error is not None and "unexpected" in error


def test_execution_config_choice_is_reflected_in_result(tool):
    args = dict(VALID_ARGS, execution_timing="NEXT_BAR_OPEN", slippage_bps=10.0, fee_bps=1.0)
    result = tool.execute(args)
    assert result.status == "ok"
    assert result.data["execution_config"]["timing"] == "NEXT_BAR_OPEN"
    assert result.data["execution_config"]["slippage"]["slippage_bps"] == 10.0
