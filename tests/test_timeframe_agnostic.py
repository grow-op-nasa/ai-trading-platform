"""Timeframe-agnostic architecture contract tests (`DECISIONS.md`,
ADR-0038).

A small number of strong, architectural tests rather than many
superficial ones -- each one proves a specific claim from the
timeframe-agnostic corrections rather than re-testing what
`tests/test_backtesting.py`/`tests/test_attribution.py`/
`tests/test_experiment_spec.py` already cover:

1. The exact same strategy code and pipeline wiring run against daily
   and 1-minute candles unmodified -- no special-case branching on
   timeframe anywhere in Strategy/Backtester/ExperimentSpec.
2. A strategy can emit more than one signal within a single trading
   session (no one-signal-per-day limit anywhere in the architecture).
3. A trade held for minutes produces a correct, non-truncated intraday
   holding period -- full timestamp precision survives all the way
   through to Performance Attribution.
4. Sharpe annualization reflects the data's actual bar frequency
   instead of a hidden "one candle = one trading day" assumption.
5. `ExperimentSpec.interval` is a typed `Interval` value, not a bare
   string a caller could misspell -- and it round-trips through the
   Experiment Registry intact.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtesting.engine import Backtester
from src.backtesting.metrics import infer_periods_per_year, sharpe_ratio
from src.data.base import Interval
from src.experiments.registry import ExperimentRegistry
from src.experiments.spec import ExperimentSpec
from src.attribution.engine import PerformanceAttributor
from src.risk.models import RiskLimits
from src.signals.models import Signal, SignalDirection
from src.strategies.ema_cross import EMACrossStrategy

# The same known-good EMA-crossover closes shape used elsewhere in the
# suite (tests/test_pipeline_contract.py, tests/test_ema_cross_strategy.py)
# -- reused here, not redesigned, so any difference in behavior between
# the daily and 1-minute runs below is attributable to the timeframe,
# not to a different price shape.
CROSSOVER_CLOSES = [110, 108, 106, 104, 102, 100, 105, 110, 115, 120, 110, 100, 90, 80]


def make_daily_candles(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D", name="timestamp")
    return _ohlcv(closes, dates)


def make_minute_candles(closes: list[float], start: str = "2024-01-02 09:30:00") -> pd.DataFrame:
    dates = pd.date_range(start, periods=len(closes), freq="min", name="timestamp")
    return _ohlcv(closes, dates)


def _ohlcv(closes: list[float], index: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        index=index,
    )


def test_daily_and_minute_candles_run_through_the_same_pipeline_unmodified(tmp_path):
    # SPY/1d and SPY/1m: the same conceptual strategy and research
    # interfaces, per the sprint's stated minimum target. No branch
    # anywhere in this test (or in the production code it calls) checks
    # "is this daily" -- the fixtures differ only in their index.
    registry = ExperimentRegistry(db_path=tmp_path / "experiments.db")
    fixtures = {
        Interval.DAY_1: make_daily_candles(CROSSOVER_CLOSES),
        Interval.MINUTE_1: make_minute_candles(CROSSOVER_CLOSES),
    }

    specs: dict[Interval, ExperimentSpec] = {}
    for interval, candles in fixtures.items():
        strategy = EMACrossStrategy(symbol="SPY", fast=2, slow=4)

        result = Backtester().run(strategy, candles)
        assert result.signals, f"sanity check: {interval} fixture must produce signals"
        assert result.trades, f"sanity check: {interval} fixture must produce trades"

        spec = ExperimentSpec.capture(
            strategy, candles, RiskLimits(), symbol="SPY", interval=interval,
            dataset_source="test-fixture",
        )
        experiment_id = registry.log_experiment(
            changed={}, metrics_before={}, metrics_after=result.metrics, decision="INCONCLUSIVE"
        )
        registry.save_spec(experiment_id, spec)
        specs[interval] = registry.get_spec(experiment_id)

    # Both experiments are reproducible and distinctly identified by
    # timeframe -- SPY/1d and SPY/1m never collapse into "the same
    # experiment" just because the strategy and symbol match.
    assert specs[Interval.DAY_1].interval is Interval.DAY_1
    assert specs[Interval.MINUTE_1].interval is Interval.MINUTE_1
    assert specs[Interval.DAY_1].dataset_fingerprint != specs[Interval.MINUTE_1].dataset_fingerprint

    # The minute fixture's recorded dataset boundaries actually carry a
    # time-of-day -- not silently collapsed to midnight/date-only.
    assert specs[Interval.MINUTE_1].dataset_start.time() != pd.Timestamp("00:00:00").time()


def test_multiple_signals_within_a_single_trading_session():
    # 09:31 -> LONG, 09:37 -> FLAT: two decisions six minutes apart,
    # both on 2024-01-02, both required to survive as distinct signals
    # and a single precisely-timed trade -- no artificial one-signal-
    # per-day/session limit anywhere in Signal, Strategy, or Backtester.
    candles = make_minute_candles([100.0] * 20, start="2024-01-02 09:30:00")
    entry_ts = candles.index[1]  # 09:31
    exit_ts = candles.index[7]  # 09:37
    assert entry_ts.date() == exit_ts.date(), "sanity check: same trading session"

    signals = [
        Signal(timestamp=entry_ts, symbol="SPY", direction=SignalDirection.LONG, confidence=0.9),
        Signal(timestamp=exit_ts, symbol="SPY", direction=SignalDirection.FLAT, confidence=0.9),
    ]

    class ScriptedStrategy:
        name = "scripted"

        def prepare(self, data):
            return data.copy()

        def generate_signals(self, data):
            return list(signals)

    result = Backtester().run(ScriptedStrategy(), candles)

    assert len(result.signals) == 2
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_time == entry_ts
    assert trade.exit_time == exit_ts
    assert trade.exit_time - trade.entry_time == pd.Timedelta(minutes=6)


def test_intraday_holding_period_is_precise_not_truncated_to_a_date():
    # The sprint's own worked example: 09:42:00 -> 09:47:30 should
    # attribute as ~5.5 minutes, not zero and not rounded into a
    # date-level bucket. Needs sub-minute candle spacing (30s) so a real
    # candle timestamp exists exactly at the 30-second exit mark.
    index = pd.date_range("2024-01-02 09:40:00", periods=20, freq="30s", name="timestamp")
    candles = _ohlcv([100.0] * len(index), index)
    entry_ts = pd.Timestamp("2024-01-02 09:42:00")
    exit_ts = pd.Timestamp("2024-01-02 09:47:30")
    assert entry_ts in candles.index and exit_ts in candles.index  # sanity check

    class ScriptedStrategy:
        name = "scripted"

        def prepare(self, data):
            return data.copy()

        def generate_signals(self, data):
            return [
                Signal(timestamp=entry_ts, symbol="SPY", direction=SignalDirection.LONG, confidence=0.9),
                Signal(timestamp=exit_ts, symbol="SPY", direction=SignalDirection.FLAT, confidence=0.9),
            ]

    result = Backtester().run(ScriptedStrategy(), candles)
    attribution = PerformanceAttributor().run(result, candles, trend_fast=2, trend_slow=4)

    assert attribution.average_hold == pd.Timedelta(minutes=5, seconds=30)
    # Not silently collapsed to zero or a whole-day unit.
    assert attribution.average_hold > pd.Timedelta(0)
    assert attribution.average_hold < pd.Timedelta(days=1)


def test_sharpe_annualization_reflects_actual_bar_frequency():
    # DECISIONS.md, ADR-0038: before this correction, every backtest's
    # Sharpe was annualized with a flat 252 regardless of bar size --
    # silently treating a 1-minute return as if it were a full trading
    # day's return. infer_periods_per_year() must scale with the data's
    # own spacing, not return the same number for both timeframes.
    daily_index = pd.date_range("2024-01-01", periods=30, freq="D")
    minute_index = pd.date_range("2024-01-02 09:30:00", periods=30, freq="min")

    daily_periods = infer_periods_per_year(daily_index)
    minute_periods = infer_periods_per_year(minute_index)

    assert daily_periods == 252  # unchanged from the historical default
    assert minute_periods is not None
    assert minute_periods > daily_periods * 100  # orders of magnitude apart, not equal

    # The same identical return series, annualized at each inferred
    # factor, produces very different Sharpe ratios -- proving the
    # annualization is actually load-bearing, not a display-only detail.
    returns = pd.Series([0.001, -0.0005, 0.0008, -0.0003, 0.0006])
    sharpe_daily = sharpe_ratio(returns, periods_per_year=daily_periods)
    sharpe_minute = sharpe_ratio(returns, periods_per_year=minute_periods)
    assert sharpe_minute > sharpe_daily * 10


def test_infer_periods_per_year_handles_degenerate_input():
    assert infer_periods_per_year(pd.DatetimeIndex([])) is None
    assert infer_periods_per_year(pd.DatetimeIndex(["2024-01-01"])) is None


def test_experiment_spec_interval_is_a_typed_value_not_a_bare_string(tmp_path):
    strategy = EMACrossStrategy(symbol="SPY", fast=2, slow=4)
    candles = make_minute_candles(CROSSOVER_CLOSES)

    spec = ExperimentSpec.capture(
        strategy, candles, RiskLimits(), symbol="SPY", interval="1m",  # plain string in
        dataset_source="test-fixture",
    )

    assert isinstance(spec.interval, Interval)
    assert spec.interval is Interval.MINUTE_1

    with pytest.raises(ValueError):
        ExperimentSpec.capture(
            strategy, candles, RiskLimits(), symbol="SPY", interval="not_a_real_interval",
            dataset_source="test-fixture",
        )

    # Round-trips through the registry as the same typed value, not a
    # plain string that happens to compare equal.
    registry = ExperimentRegistry(db_path=tmp_path / "experiments.db")
    experiment_id = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="INCONCLUSIVE"
    )
    registry.save_spec(experiment_id, spec)
    fetched = registry.get_spec(experiment_id)
    assert isinstance(fetched.interval, Interval)
    assert fetched.interval is Interval.MINUTE_1
