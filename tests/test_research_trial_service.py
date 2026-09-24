"""Tests for `src.research.trial_service.ResearchTrialService` -- Sprint
13 spec, sections 23-25 (Phase 6), 76: the generic research
orchestration service the `run_historical_backtest` agent tool (and any
other caller) uses, composing MarketDataService -> Strategy ->
PortfolioBacktestEngine -> Risk -> Execution -> Analytics with no second
backtest pipeline. No network access -- a fake `MarketDataService` is
injected everywhere a dataset is needed.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

pytest.importorskip("joblib")  # transitively required by src.ai.registry.ModelRegistry

from src.data.base import Interval
from src.data.models import CandleDataset, SessionPolicy, ValidationReport
from src.research.trial_service import ResearchTrialService
from src.strategies.ema_cross import EMACrossStrategy  # noqa: F401 -- registers "ema_cross"
from src.utils.hashing import dataframe_fingerprint


def _candles(n: int = 60) -> pd.DataFrame:
    closes = [100.0 + (i % 7) - 3 + i * 0.1 for i in range(n)]
    dates = pd.date_range("2024-01-01", periods=n, freq="D", name="timestamp", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
         "close": closes, "volume": [1000.0] * n},
        index=dates,
    )


class FakeMarketDataService:
    """Deterministic, network-free stand-in for `MarketDataService` --
    returns the same synthetic dataset for any request."""

    def __init__(self, candles: pd.DataFrame | None = None) -> None:
        self._candles = candles if candles is not None else _candles()

    def get_dataset(self, symbol, start=None, end=None, interval=Interval.DAY_1, **kwargs) -> CandleDataset:
        interval = Interval(interval)
        return CandleDataset(
            symbol=symbol,
            interval=interval,
            provider="fake",
            candles=self._candles,
            content_hash="fake-content-hash",
            validation_report=ValidationReport(symbol=symbol, interval=interval),
            session_policy=SessionPolicy.REGULAR,
            session_timezone="America/New_York",
            requested_start=start or date(2024, 1, 1),
            requested_end=end or date(2024, 3, 1),
            dataset_start=self._candles.index.min(),
            dataset_end=self._candles.index.max(),
        )


def test_run_trial_produces_full_provenance(tmp_path):
    candles = _candles()
    service = ResearchTrialService(market_data_service=FakeMarketDataService(candles))
    outcome = service.run_trial(
        strategy_name="ema_cross",
        strategy_params={"fast": 3, "slow": 8},
        symbol="SPY",
        interval="1d",
        start=date(2024, 1, 1),
        end=date(2024, 3, 1),
    )
    assert outcome.trial_id
    assert outcome.spec.symbol == "SPY"
    assert outcome.spec.strategy_name == "ema_cross"
    # ExperimentSpec.capture() always hashes the actual candles used
    # (src.utils.hashing.dataframe_fingerprint) -- it never trusts a
    # CandleDataset's own `content_hash` field, so the fixture's
    # "fake-content-hash" placeholder is never what ends up here.
    assert outcome.spec.dataset_fingerprint == dataframe_fingerprint(candles)
    assert outcome.analytics.symbol == "SPY"
    assert outcome.result.risk_mode.value == "PORTFOLIO_RISK"
    assert outcome.model_id is None


def test_run_trial_rejects_future_end_date():
    service = ResearchTrialService(market_data_service=FakeMarketDataService())
    with pytest.raises(ValueError, match="future"):
        service.run_trial(
            strategy_name="ema_cross", strategy_params={}, symbol="SPY", interval="1d",
            start=date(2024, 1, 1), end=date.today() + timedelta(days=5),
        )


def test_run_trial_rejects_start_after_end():
    service = ResearchTrialService(market_data_service=FakeMarketDataService())
    with pytest.raises(ValueError, match="must not be after"):
        service.run_trial(
            strategy_name="ema_cross", strategy_params={}, symbol="SPY", interval="1d",
            start=date(2024, 3, 1), end=date(2024, 1, 1),
        )


def test_run_trial_unknown_strategy_raises_keyerror():
    service = ResearchTrialService(market_data_service=FakeMarketDataService())
    with pytest.raises(KeyError):
        service.run_trial(
            strategy_name="does_not_exist", strategy_params={}, symbol="SPY", interval="1d",
            start=date(2024, 1, 1), end=date(2024, 3, 1),
        )


def test_run_trial_execution_config_is_recorded_in_spec_backtest_config():
    service = ResearchTrialService(market_data_service=FakeMarketDataService())
    outcome = service.run_trial(
        strategy_name="ema_cross", strategy_params={}, symbol="SPY", interval="1d",
        start=date(2024, 1, 1), end=date(2024, 3, 1),
        execution_timing="NEXT_BAR_OPEN", slippage_bps=5.0, fee_bps=2.0,
    )
    execution = outcome.spec.backtest_config["execution"]
    assert execution["timing"] == "NEXT_BAR_OPEN"
    assert execution["slippage"]["slippage_bps"] == 5.0
    assert execution["fee"]["fee_bps"] == 2.0


def test_run_trial_is_reproducible_for_identical_inputs():
    service = ResearchTrialService(market_data_service=FakeMarketDataService())
    kwargs = dict(
        strategy_name="ema_cross", strategy_params={"fast": 3, "slow": 8}, symbol="SPY",
        interval="1d", start=date(2024, 1, 1), end=date(2024, 3, 1),
    )
    outcome_a = service.run_trial(**kwargs)
    outcome_b = service.run_trial(**kwargs)
    assert outcome_a.spec.dataset_fingerprint == outcome_b.spec.dataset_fingerprint
    assert outcome_a.analytics.total_pnl.value == outcome_b.analytics.total_pnl.value
    assert outcome_a.analytics.trade_count == outcome_b.analytics.trade_count
    # trial_id is a fresh identifier every call -- an event, not a spec.
    assert outcome_a.trial_id != outcome_b.trial_id
