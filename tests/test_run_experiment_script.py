"""Tests for the worked-example script (`ROADMAP.md`, Sprint 6
close-out item 1): `scripts/run_experiment.py`.

Exercises `run_experiment()` directly against synthetic, network-free
candles -- proving the full wiring (Strategy -> Backtest -> Risk ->
Execution -> Attribution -> Research Report -> Experiment Registry)
works as a real, callable function, not just inside a test's own
hand-rolled pipeline (`tests/test_pipeline_contract.py`). Run against
*two* different registered strategies deliberately -- the same proof
point as `tests/test_rsi_mean_reversion_strategy.py`: the wiring
doesn't care which strategy it's handed.

`main()` and its network-touching `MarketDataService` call are
intentionally not exercised here -- there is no network access in the
sandbox this suite runs in, and `run_experiment()` was designed
specifically so that isn't necessary to test the wiring itself.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.data.base import Interval
from src.experiments.registry import ExperimentRegistry
from src.risk.models import RiskLimits
from scripts.run_experiment import ExperimentRunResult, _parse_args, run_experiment

# Same known-good EMA-crossover shape already proven in
# tests/test_ema_cross_strategy.py and tests/test_pipeline_contract.py.
EMA_CLOSES = [110, 108, 106, 104, 102, 100, 105, 110, 115, 120, 110, 100, 90, 80]

# Same empirically-verified oversold-to-overbought cycle used in
# tests/test_rsi_mean_reversion_strategy.py.
RSI_CLOSES = [100 - i * 2 for i in range(15)] + [70 + i * 3 for i in range(15)]


def make_candles(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D", name="timestamp")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        index=dates,
    )


def test_run_experiment_with_ema_cross(tmp_path):
    candles = make_candles(EMA_CLOSES)
    registry = ExperimentRegistry(db_path=tmp_path / "experiments.db")

    run_result = run_experiment(
        strategy_name="ema_cross",
        symbol="SPY",
        candles=candles,
        strategy_params={"fast": 2, "slow": 4},
        registry=registry,
    )

    assert isinstance(run_result, ExperimentRunResult)
    assert run_result.result.trades  # sanity check: this fixture must produce trades
    assert run_result.spec.strategy_name == "ema_cross"
    assert run_result.spec.symbol == "SPY"
    # interval defaults to "1d" (a plain string) and comes back as the
    # typed Interval -- ExperimentSpec's own normalization, not
    # something run_experiment() has to do itself (DECISIONS.md, ADR-0038).
    assert run_result.spec.interval == Interval.DAY_1
    assert run_result.attribution.total_trades == len(run_result.result.trades)
    assert run_result.report.findings is not None

    # Persisted, not just returned.
    fetched_spec = registry.get_spec(run_result.experiment_id)
    assert fetched_spec == run_result.spec
    fetched_signals = registry.get_signals(run_result.experiment_id)
    assert {s.id for s in fetched_signals} == {s.id for s in run_result.result.signals}

    # Sprint 9 (DECISIONS.md, ADR-0042): trades, equity curve, the
    # resulting Portfolio, and the research report are all persisted
    # too, not just returned in-memory (ExperimentRunResult).
    assert registry.get_trades(run_result.experiment_id) == run_result.result.trades
    fetched_curve = registry.get_equity_curve(run_result.experiment_id)
    assert list(fetched_curve.values) == list(run_result.result.equity_curve.values)
    fetched_portfolio = registry.get_portfolio(run_result.experiment_id)
    assert fetched_portfolio is not None
    assert fetched_portfolio.cash == run_result.portfolio.cash
    fetched_report = registry.get_research_report(run_result.experiment_id)
    assert fetched_report == run_result.report


def test_run_experiment_returns_a_portfolio_reflecting_paper_executed_trades(tmp_path):
    from src.portfolio.models import Portfolio

    candles = make_candles(EMA_CLOSES)
    registry = ExperimentRegistry(db_path=tmp_path / "experiments.db")

    run_result = run_experiment(
        strategy_name="ema_cross",
        symbol="SPY",
        candles=candles,
        strategy_params={"fast": 2, "slow": 4},
        registry=registry,
    )

    assert isinstance(run_result.portfolio, Portfolio)
    # Every trade the backtester extracted corresponds to one closed
    # position once paper-executed through Risk -> Execution -> Portfolio
    # (an open position at the very end of the run, if any, is the one
    # exception -- it has no corresponding backtest-side "close").
    closed_count = len(run_result.portfolio.closed_positions)
    open_count = len(run_result.portfolio.positions)
    assert closed_count + open_count == len(run_result.result.trades)


def test_run_experiment_with_rsi_mean_reversion(tmp_path):
    # The same function, unmodified, run against a genuinely different
    # strategy -- proving run_experiment() itself doesn't secretly
    # assume EMACrossStrategy's shape.
    candles = make_candles(RSI_CLOSES)
    registry = ExperimentRegistry(db_path=tmp_path / "experiments.db")

    run_result = run_experiment(
        strategy_name="rsi_mean_reversion",
        symbol="QQQ",
        candles=candles,
        strategy_params={"period": 5},
        registry=registry,
    )

    assert run_result.result.trades
    assert run_result.spec.strategy_name == "rsi_mean_reversion"
    assert run_result.spec.strategy_params == {
        "period": 5,
        "oversold": 30.0,
        "overbought": 70.0,
        "confidence": 0.6,
    }
    assert run_result.spec.symbol == "QQQ"

    fetched_spec = registry.get_spec(run_result.experiment_id)
    assert fetched_spec == run_result.spec


def test_run_experiment_respects_custom_risk_limits(tmp_path):
    candles = make_candles(EMA_CLOSES)
    registry = ExperimentRegistry(db_path=tmp_path / "experiments.db")
    risk_limits = RiskLimits(allocation_per_trade_pct=0.25, max_portfolio_exposure_pct=0.75)

    run_result = run_experiment(
        strategy_name="ema_cross",
        symbol="SPY",
        candles=candles,
        strategy_params={"fast": 2, "slow": 4},
        risk_limits=risk_limits,
        registry=registry,
    )

    assert run_result.spec.risk_config == {
        "allocation_per_trade_pct": 0.25,
        "max_portfolio_exposure_pct": 0.75,
    }


def test_run_experiment_rejects_unregistered_strategy_name(tmp_path):
    candles = make_candles(EMA_CLOSES)
    registry = ExperimentRegistry(db_path=tmp_path / "experiments.db")

    with pytest.raises(KeyError):
        run_experiment(
            strategy_name="not_a_real_strategy",
            symbol="SPY",
            candles=candles,
            registry=registry,
        )


def test_parse_args_defaults():
    args = _parse_args([])
    assert args.symbol == "SPY"
    assert args.strategy == "ema_cross"
    assert args.period == "2y"
    assert args.interval == "1d"
    assert args.allocation == 0.10


def test_parse_args_explicit_values():
    args = _parse_args(
        [
            "--symbol", "QQQ",
            "--strategy", "rsi_mean_reversion",
            "--period", "6mo",
            "--interval", "1m",
            "--allocation", "0.2",
        ]
    )
    assert args.symbol == "QQQ"
    assert args.strategy == "rsi_mean_reversion"
    assert args.period == "6mo"
    assert args.interval == "1m"
    assert args.allocation == 0.2


def test_parse_args_rejects_unrecognized_interval():
    with pytest.raises(SystemExit):
        _parse_args(["--interval", "not_a_real_interval"])


def test_parse_args_rejects_unregistered_strategy_choice():
    with pytest.raises(SystemExit):
        _parse_args(["--strategy", "not_a_real_strategy"])
