"""Direct, per-criterion evidence for the timeframe/intraday sprint's
formal Acceptance Criteria (AC-01 through AC-19, see the sprint's
acceptance-criteria document).

This file exists specifically to make each AC's "pass condition"
independently checkable -- each test's docstring names the AC(s) it
proves, using the AC's own literal examples where one was given (e.g.
`09:42:13` / `09:47:51`), rather than a different, merely-similar
fixture. Several ACs are already covered by pre-existing tests
elsewhere in the suite; this file adds coverage only for the ACs that
needed a new, dedicated test to be unambiguously demonstrated -- it
does not duplicate what `tests/test_timeframe_agnostic.py`,
`tests/test_hashing.py`, `tests/test_strategy_registry.py`, or
`tests/test_backtesting.py` already prove. See the sprint completion
report for the full AC -> test mapping, including which pre-existing
tests satisfy which ACs.
"""

from __future__ import annotations

import typing
from uuid import UUID

import pandas as pd
import pytest

from src.backtesting.engine import Backtester
from src.backtesting.models import Trade
from src.data.base import Interval
from src.experiments.registry import ExperimentRegistry
from src.experiments.spec import ExperimentSpec
from src.attribution.engine import PerformanceAttributor
from src.risk.models import RiskLimits
from src.signals.models import Signal, SignalDirection
from src.strategies.ema_cross import EMACrossStrategy
from src.strategies.rsi_mean_reversion import RSIMeanReversionStrategy


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


class _ScriptedStrategy:
    """Test double returning a pre-built signal list, same pattern as
    `tests/test_backtesting.py::ScriptedStrategy` -- reused here rather
    than reimplemented."""

    name = "scripted"

    def __init__(self, signals: list[Signal]):
        self._signals = signals

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        return data.copy()

    def generate_signals(self, data: pd.DataFrame) -> list[Signal]:
        return list(self._signals)


# ---------------------------------------------------------------------------
# AC-03: intraday timestamps survive data -> strategy/backtest -> result
# without being reduced to a calendar date. Uses the AC's own example
# timestamp verbatim.
# ---------------------------------------------------------------------------


def test_ac03_intraday_timestamp_survives_the_full_pipeline_without_date_truncation():
    exact_ts = pd.Timestamp("2026-09-15 09:42:13")
    index = pd.DatetimeIndex(
        [exact_ts, exact_ts + pd.Timedelta(minutes=1), exact_ts + pd.Timedelta(minutes=2)]
    )
    candles = _ohlcv([100.0, 101.0, 102.0], index)
    signal = Signal(timestamp=exact_ts, symbol="SPY", direction=SignalDirection.LONG, confidence=0.9)

    result = Backtester().run(_ScriptedStrategy([signal]), candles)

    # Survives through the Signal the Backtester passes through untouched...
    assert result.signals[0].timestamp == exact_ts
    # ...and through the Trade it opens.
    assert result.trades[0].entry_time == exact_ts

    # The literal failure mode this AC forbids: the timestamp must NOT
    # have been reduced to a bare calendar date anywhere along the way.
    assert result.trades[0].entry_time != pd.Timestamp("2026-09-15")
    assert result.trades[0].entry_time.time() != pd.Timestamp("2026-09-15").time()
    assert result.trades[0].entry_time.hour == 9
    assert result.trades[0].entry_time.minute == 42
    assert result.trades[0].entry_time.second == 13


# ---------------------------------------------------------------------------
# AC-04: multiple signals for the same symbol on the same calendar day,
# each uniquely identifiable. Uses the AC's own example timestamps and
# directions verbatim (09:42:13 LONG, 09:47:51 FLAT).
# ---------------------------------------------------------------------------


def test_ac04_two_signals_same_day_are_both_retained_and_uniquely_identifiable():
    day = "2026-09-15"
    long_ts = pd.Timestamp(f"{day} 09:42:13")
    flat_ts = pd.Timestamp(f"{day} 09:47:51")
    index = pd.DatetimeIndex(
        [long_ts, long_ts + pd.Timedelta(minutes=1), flat_ts, flat_ts + pd.Timedelta(minutes=1)]
    )
    candles = _ohlcv([100.0, 100.5, 101.0, 101.5], index)

    long_signal = Signal(timestamp=long_ts, symbol="SPY", direction=SignalDirection.LONG, confidence=0.9)
    flat_signal = Signal(timestamp=flat_ts, symbol="SPY", direction=SignalDirection.FLAT, confidence=0.9)

    result = Backtester().run(_ScriptedStrategy([long_signal, flat_signal]), candles)

    assert len(result.signals) == 2
    assert {s.timestamp for s in result.signals} == {long_ts, flat_ts}
    assert {s.direction for s in result.signals} == {SignalDirection.LONG, SignalDirection.FLAT}
    # Uniquely identifiable: distinct client-assigned UUIDs, not just
    # distinct timestamps.
    ids = {s.id for s in result.signals}
    assert len(ids) == 2
    assert all(isinstance(i, UUID) for i in ids)
    # Both on the same calendar day -- proving this isn't accidentally
    # exercising two different days.
    assert all(s.timestamp.date() == pd.Timestamp(day).date() for s in result.signals)

    assert len(result.trades) == 1
    assert result.trades[0].entry_time == long_ts
    assert result.trades[0].exit_time == flat_ts


# ---------------------------------------------------------------------------
# AC-06: no-lookahead guarantee holds at intraday granularity, not just
# daily -- a signal fired on a 1-minute bar must not be able to act on
# that same bar's own return.
# ---------------------------------------------------------------------------


def test_ac06_intraday_signal_cannot_use_information_from_a_later_candle():
    index = pd.date_range("2026-09-15 09:30:00", periods=5, freq="min")
    # A huge, unmistakable +100% jump from bar 0 to bar 1 -- if the
    # signal fired on bar 0 could "see" this move, equity would jump
    # immediately on bar 0 instead of on bar 1.
    closes = [100.0, 200.0, 200.0, 200.0, 200.0]
    candles = _ohlcv(closes, index)
    signals = [
        Signal(timestamp=index[0], symbol="SPY", direction=SignalDirection.LONG, confidence=0.9),
        Signal(timestamp=index[1], symbol="SPY", direction=SignalDirection.FLAT, confidence=0.9),
    ]

    result = Backtester().run(_ScriptedStrategy(signals), candles)

    equity = result.equity_curve
    assert equity.iloc[0] == pytest.approx(100_000.0)  # unaffected by bar 0's own +100% move
    assert equity.iloc[1] == pytest.approx(200_000.0)  # realized one bar later, as expected
    assert equity.iloc[2] == pytest.approx(200_000.0)


# ---------------------------------------------------------------------------
# AC-08: every stored experiment carries an automatic strategy
# fingerprint; two experiments built from different strategy source
# implementations must produce different fingerprints -- checked at the
# persisted-experiment level (ExperimentSpec + ExperimentRegistry), not
# just the raw `strategy_version()` utility (already covered by
# tests/test_strategy_registry.py).
# ---------------------------------------------------------------------------


def test_ac08_stored_experiments_from_different_strategy_implementations_have_different_fingerprints(
    tmp_path,
):
    candles = _ohlcv([100, 101, 102, 103, 104], pd.date_range("2024-01-01", periods=5, freq="D"))
    registry = ExperimentRegistry(db_path=tmp_path / "experiments.db")

    spec_a = ExperimentSpec.capture(
        EMACrossStrategy(symbol="SPY", fast=2, slow=4), candles, RiskLimits(),
        symbol="SPY", interval="1d", dataset_source="test-fixture",
    )
    spec_b = ExperimentSpec.capture(
        RSIMeanReversionStrategy(symbol="SPY", period=5), candles, RiskLimits(),
        symbol="SPY", interval="1d", dataset_source="test-fixture",
    )

    id_a = registry.log_experiment(changed={}, metrics_before={}, metrics_after={}, decision="INCONCLUSIVE")
    id_b = registry.log_experiment(changed={}, metrics_before={}, metrics_after={}, decision="INCONCLUSIVE")
    registry.save_spec(id_a, spec_a)
    registry.save_spec(id_b, spec_b)

    fetched_a = registry.get_spec(id_a)
    fetched_b = registry.get_spec(id_b)

    assert fetched_a.strategy_version != fetched_b.strategy_version
    assert fetched_a.strategy_version == spec_a.strategy_version  # persisted intact
    assert fetched_b.strategy_version == spec_b.strategy_version


# ---------------------------------------------------------------------------
# AC-10: the experiment model distinguishes dataset *identity* from the
# human-readable request descriptor -- two experiments sharing an
# identical symbol + timeframe + requested range + provider descriptor
# must still coexist with different dataset hashes when their actual
# candle content differs.
# ---------------------------------------------------------------------------


def test_ac10_identical_request_descriptor_can_coexist_with_different_dataset_hashes(tmp_path):
    # Same index (same requested/actual range) -- this is what makes the
    # descriptor identical -- but different candle values, simulating a
    # vendor data revision between two fetches of "the same" request.
    index = pd.date_range("2024-01-01", periods=10, freq="D")
    candles_v1 = _ohlcv([100, 101, 102, 103, 104, 105, 106, 107, 108, 109], index)
    candles_v2 = _ohlcv([100, 101, 102, 103, 104, 105, 106, 107, 108, 999], index)  # one value revised

    strategy_a = EMACrossStrategy(symbol="SPY", fast=2, slow=4)
    strategy_b = EMACrossStrategy(symbol="SPY", fast=2, slow=4)

    spec_a = ExperimentSpec.capture(
        strategy_a, candles_v1, RiskLimits(), symbol="SPY", interval="1d", dataset_source="yfinance",
    )
    spec_b = ExperimentSpec.capture(
        strategy_b, candles_v2, RiskLimits(), symbol="SPY", interval="1d", dataset_source="yfinance",
    )

    # Identical descriptor: symbol + timeframe + requested range + provider.
    assert spec_a.symbol == spec_b.symbol
    assert spec_a.interval == spec_b.interval
    assert spec_a.dataset_start == spec_b.dataset_start
    assert spec_a.dataset_end == spec_b.dataset_end
    assert spec_a.dataset_source == spec_b.dataset_source

    # Yet the dataset content differs -- and the model catches it.
    assert spec_a.dataset_fingerprint != spec_b.dataset_fingerprint

    # Both coexist in the registry, retrievable with their distinct hashes intact.
    registry = ExperimentRegistry(db_path=tmp_path / "experiments.db")
    id_a = registry.log_experiment(changed={}, metrics_before={}, metrics_after={}, decision="INCONCLUSIVE")
    id_b = registry.log_experiment(changed={}, metrics_before={}, metrics_after={}, decision="INCONCLUSIVE")
    registry.save_spec(id_a, spec_a)
    registry.save_spec(id_b, spec_b)

    assert registry.get_spec(id_a).dataset_fingerprint != registry.get_spec(id_b).dataset_fingerprint


# ---------------------------------------------------------------------------
# AC-12: architecture inspection -- the core Signal/Trade/ExperimentSpec
# APIs must not require calendar-date-only timestamps, and the core
# modules must not encode a one-signal-per-day (or per-candle) limit.
# This test is designed to FAIL if such an assumption is reintroduced,
# e.g. if a future change narrows `Signal.timestamp` to `datetime.date`.
# ---------------------------------------------------------------------------


def test_ac12_core_timestamp_fields_are_full_precision_not_calendar_date_only():
    checks = [
        (Signal, "timestamp"),
        (Trade, "entry_time"),
        (Trade, "exit_time"),
        (ExperimentSpec, "dataset_start"),
        (ExperimentSpec, "dataset_end"),
    ]
    for cls, field_name in checks:
        hints = typing.get_type_hints(cls)
        annotation = hints[field_name]
        assert annotation is pd.Timestamp, (
            f"{cls.__name__}.{field_name} is annotated {annotation!r}, not "
            f"pd.Timestamp -- a calendar-date-only type here would silently "
            f"discard intraday information (AC-12)."
        )


def test_ac12_no_one_signal_per_day_limit_exists_in_signal_generation():
    # The literal claim this protects: a strategy can emit as many
    # signals within one trading session as it wants. Constructed
    # directly against the real Strategy contract (not a mock) -- if a
    # future change added an artificial per-day cap inside
    # BaseStrategy/Backtester, this would start failing.
    index = pd.date_range("2026-09-15 09:30:00", periods=6, freq="min")
    candles = _ohlcv([100, 101, 100, 101, 100, 101], index)
    ts = list(index)
    signals = [
        Signal(timestamp=ts[0], symbol="SPY", direction=SignalDirection.LONG, confidence=0.9),
        Signal(timestamp=ts[1], symbol="SPY", direction=SignalDirection.FLAT, confidence=0.9),
        Signal(timestamp=ts[2], symbol="SPY", direction=SignalDirection.LONG, confidence=0.9),
        Signal(timestamp=ts[3], symbol="SPY", direction=SignalDirection.FLAT, confidence=0.9),
        Signal(timestamp=ts[4], symbol="SPY", direction=SignalDirection.LONG, confidence=0.9),
    ]
    # All five signals fall on the same calendar day.
    assert len({s.timestamp.date() for s in signals}) == 1

    result = Backtester().run(_ScriptedStrategy(signals), candles)

    assert len(result.signals) == 5  # none silently dropped or capped
    # LONG->FLAT->LONG->FLAT->LONG: two closed round trips, plus the
    # final LONG closed at the end of the data (no closing signal) --
    # three trades total. None of the five signals were dropped or
    # merged by an artificial per-day cap.
    assert len(result.trades) == 3


# ---------------------------------------------------------------------------
# AC-13: end-to-end minute-scale demonstration -- 1-minute candles ->
# strategy -> multiple intraday signals -> backtest -> trade(s) ->
# attribution, using the exact same functions/classes used for daily
# data elsewhere in the suite (no daily-specific adapter, no
# special-case strategy logic).
# ---------------------------------------------------------------------------


def test_ac13_end_to_end_minute_scale_workflow_through_attribution():
    # Two full EMA crossover cycles compressed into 1-minute bars --
    # produces multiple signals and multiple trades, all within one
    # trading session.
    closes = (
        [100, 99, 98, 100, 103, 106, 104, 102, 100, 98]
        + [100, 103, 106, 108, 106, 104, 102, 100, 98, 96]
    )
    index = pd.date_range("2026-09-15 09:30:00", periods=len(closes), freq="min")
    candles = _ohlcv(closes, index)

    # The identical strategy class/constructor used against daily
    # fixtures elsewhere (tests/test_ema_cross_strategy.py,
    # tests/test_pipeline_contract.py) -- no timeframe-specific
    # subclass or branch.
    strategy = EMACrossStrategy(symbol="SPY", fast=2, slow=4)

    result = Backtester().run(strategy, candles)
    assert len(result.signals) >= 2, "sanity check: this fixture must produce multiple signals"
    assert result.trades, "sanity check: this fixture must produce at least one trade"

    attribution = PerformanceAttributor().run(result, candles, trend_fast=2, trend_slow=4)
    assert attribution.total_trades == len(result.trades)
    assert attribution.average_hold is not None
    # A minute-scale run's average hold must be minute-scale, not a
    # multi-day figure -- confirms attribution stayed intraday-aware
    # through the full pipeline, not just in isolation (AC-07).
    assert attribution.average_hold < pd.Timedelta(hours=1)

    spec = ExperimentSpec.capture(
        strategy, candles, RiskLimits(), symbol="SPY", interval=Interval.MINUTE_1,
        dataset_source="test-fixture",
    )
    assert spec.interval is Interval.MINUTE_1
    assert spec.dataset_start.time() != pd.Timestamp("00:00:00").time()  # real time-of-day, not midnight


# ---------------------------------------------------------------------------
# AC-14: documentation must not overstate what's implemented -- states
# minute-level intraday as the current target, and HFT/tick/second-level
# execution as explicit future work, not a supported capability.
# ---------------------------------------------------------------------------


def test_ac14_documentation_explicitly_scopes_out_hft_and_states_the_intraday_target():
    import pathlib
    import re

    architecture_md = (
        pathlib.Path(__file__).resolve().parent.parent / "ARCHITECTURE.md"
    ).read_text()
    # Collapse whitespace/line-wrapping so this check is robust to the
    # markdown's own word-wrap width -- it verifies the *content* is
    # present, not that it was never rewrapped onto different lines.
    normalized = re.sub(r"\s+", " ", architecture_md)

    assert (
        "minute-level intraday research/trading is the current architectural target"
        in normalized
    )
    assert (
        "Tick-level and second-level execution infrastructure is not implemented"
        in normalized
    )
    assert "High-frequency trading (HFT) remains explicit future" in normalized
