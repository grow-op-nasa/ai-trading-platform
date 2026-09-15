"""Tests for the experiment specification (src/experiments/spec.py --
`DECISIONS.md`, ADR-0035).
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.experiments.spec import ExperimentSpec
from src.risk.models import RiskLimits
from src.strategies.ema_cross import EMACrossStrategy
from src.utils.hashing import dataframe_fingerprint


def make_candles(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D", name="timestamp")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        index=dates,
    )


def make_spec(**overrides) -> ExperimentSpec:
    defaults = dict(
        strategy_name="ema_cross",
        strategy_version="deadbeef",
        strategy_params={"fast": 12, "slow": 26, "confidence": 0.7},
        symbol="SPY",
        interval="1d",
        dataset_start=pd.Timestamp("2024-01-01"),
        dataset_end=pd.Timestamp("2024-01-10"),
        dataset_source="yfinance",
        dataset_fingerprint="cafebabe",
        risk_config={"allocation_per_trade_pct": 0.1, "max_portfolio_exposure_pct": 0.5},
    )
    defaults.update(overrides)
    return ExperimentSpec(**defaults)


def test_valid_spec_constructs():
    spec = make_spec()
    assert spec.strategy_name == "ema_cross"
    assert spec.backtest_config == {}


def test_rejects_empty_string_fields():
    # Plain loop rather than pytest.mark.parametrize -- consistent with
    # this codebase's convention (see tests/test_reconciliation.py).
    for field_name in (
        "strategy_name",
        "strategy_version",
        "symbol",
        "interval",
        "dataset_source",
        "dataset_fingerprint",
    ):
        with pytest.raises(ValueError):
            make_spec(**{field_name: ""})


def test_rejects_dataset_start_after_dataset_end():
    with pytest.raises(ValueError):
        make_spec(
            dataset_start=pd.Timestamp("2024-01-10"),
            dataset_end=pd.Timestamp("2024-01-01"),
        )


def test_spec_is_immutable():
    spec = make_spec()
    with pytest.raises(Exception):
        spec.symbol = "QQQ"  # type: ignore[misc]


def test_capture_derives_strategy_identity_from_the_instance():
    strategy = EMACrossStrategy(symbol="SPY", fast=12, slow=26, confidence=0.7)
    candles = make_candles([100, 101, 102, 103, 104])

    spec = ExperimentSpec.capture(
        strategy,
        candles,
        RiskLimits(),
        symbol="SPY",
        interval="1d",
        dataset_source="yfinance",
    )

    assert spec.strategy_name == "ema_cross"
    assert spec.strategy_params == {"fast": 12, "slow": 26, "confidence": 0.7}
    assert spec.symbol == "SPY"
    assert spec.dataset_start == candles.index.min()
    assert spec.dataset_end == candles.index.max()
    assert spec.dataset_fingerprint == dataframe_fingerprint(candles)
    assert spec.risk_config == {
        "allocation_per_trade_pct": RiskLimits().allocation_per_trade_pct,
        "max_portfolio_exposure_pct": RiskLimits().max_portfolio_exposure_pct,
    }


def test_capture_honors_explicit_strategy_params_override():
    strategy = EMACrossStrategy(symbol="SPY", fast=12, slow=26)
    candles = make_candles([100, 101, 102])

    spec = ExperimentSpec.capture(
        strategy,
        candles,
        RiskLimits(),
        symbol="SPY",
        interval="1d",
        dataset_source="yfinance",
        strategy_params={"fast": 999},
    )

    assert spec.strategy_params == {"fast": 999}


def test_reconstruct_strategy_rebuilds_an_equivalent_instance():
    strategy = EMACrossStrategy(symbol="SPY", fast=5, slow=15, confidence=0.6)
    candles = make_candles([100, 101, 102])
    spec = ExperimentSpec.capture(
        strategy, candles, RiskLimits(), symbol="SPY", interval="1d", dataset_source="yfinance"
    )

    rebuilt = spec.reconstruct_strategy()

    assert isinstance(rebuilt, EMACrossStrategy)
    assert rebuilt.symbol == "SPY"
    assert rebuilt.params == {"fast": 5, "slow": 15, "confidence": 0.6}


def test_reconstruct_strategy_raises_for_unregistered_name():
    spec = make_spec(strategy_name="totally_unregistered_strategy")
    with pytest.raises(KeyError):
        spec.reconstruct_strategy()


def test_verify_strategy_version_true_when_source_unchanged():
    strategy = EMACrossStrategy(symbol="SPY", fast=12, slow=26)
    candles = make_candles([100, 101, 102])
    spec = ExperimentSpec.capture(
        strategy, candles, RiskLimits(), symbol="SPY", interval="1d", dataset_source="yfinance"
    )

    assert spec.verify_strategy_version() is True


def test_verify_strategy_version_false_when_recorded_version_is_stale():
    spec = make_spec(strategy_version="not-the-real-hash")
    assert spec.verify_strategy_version() is False


def test_verify_dataset_true_for_identical_candles():
    candles = make_candles([100, 101, 102])
    spec = make_spec(dataset_fingerprint=dataframe_fingerprint(candles))
    assert spec.verify_dataset(candles) is True


def test_verify_dataset_false_when_data_has_changed():
    original = make_candles([100, 101, 102])
    spec = make_spec(dataset_fingerprint=dataframe_fingerprint(original))

    revised = original.copy()
    revised.loc[revised.index[0], "close"] = 12345.0

    assert spec.verify_dataset(revised) is False
