"""Tests for the platform's third permanent strategy
(src/strategies/ema_cross_vol_filter.py) -- the first ever promoted out
of the Strategy Development Agent (Sprint 14, `DECISIONS.md`
ADR-0047/ADR-0048).

Mirrors `tests/test_ema_cross_strategy.py`/`test_rsi_mean_reversion_
strategy.py`'s structure and rigor -- deliberately, since the whole
point of registering a promoted candidate here is to prove it behaves
exactly like every other permanent strategy from the platform's own
point of view: same SDK, same registry, same `Backtester`, same
`ExperimentSpec` reconstruction seam. Nothing about this strategy's
AI origin is special-cased anywhere in the assertions below.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtesting.engine import Backtester
from src.indicators.engine import IndicatorEngine
from src.signals.models import SignalDirection
from src.strategies.ema_cross_vol_filter import EMACrossVolFilterStrategy
from src.strategies.registry import get_strategy_class


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


# Gentle warmup (low true range, keeps ATR% below any reasonable
# threshold) -> a sharp multi-day rally (big day-to-day jumps inflate
# ATR% well above threshold, while the fast EMA pulls above the slow
# EMA) -> a return to gentle drift, still trending up (ATR% decays back
# below threshold before the trend itself ever reverses) -> a decline.
# Verified empirically against the actual EMA/ATR formulas below (see
# test_prepare_adds_expected_columns_matching_indicator_engine), not
# hand-derived -- exactly the existing convention in this test suite
# (see test_rsi_mean_reversion_strategy.py's own CYCLE_CLOSES comment).
_QUIET_WARMUP = [100 + i * 0.05 for i in range(30)]
_BURST_UP = [_QUIET_WARMUP[-1] + i * 3 for i in range(1, 11)]

# Scenario A: volatility collapses back below threshold while the
# trend is still intact -- the exit must fire on the volatility filter
# alone, with no trend reversal anywhere in the series.
_QUIET_DRIFT_UP = [_BURST_UP[-1] + i * 0.05 for i in range(1, 20)]
_GENTLE_DECLINE = [_QUIET_DRIFT_UP[-1] - i * 3 for i in range(1, 11)]
VOL_COLLAPSE_EXIT_CLOSES = _QUIET_WARMUP + _BURST_UP + _QUIET_DRIFT_UP + _GENTLE_DECLINE

# Scenario B: volatility stays elevated (big jumps continue in both
# directions) -- the exit must fire on the trend reversal, with ATR%
# never dropping back below threshold anywhere in the series.
_CONTINUED_BURST_UP = [_BURST_UP[-1] + i * 3 for i in range(1, 6)]
_SHARP_DECLINE = [_CONTINUED_BURST_UP[-1] - i * 4 for i in range(1, 11)]
TREND_REVERSAL_EXIT_CLOSES = _QUIET_WARMUP + _BURST_UP + _CONTINUED_BURST_UP + _SHARP_DECLINE

DEFAULT_KWARGS = dict(fast=3, slow=8, atr_period=5, min_atr_pct=0.01)


def test_name_is_ema_cross_vol_filter():
    assert EMACrossVolFilterStrategy(symbol="QQQ").name == "ema_cross_vol_filter"


def test_prepare_adds_expected_columns_matching_indicator_engine():
    candles = make_candles(VOL_COLLAPSE_EXIT_CLOSES)
    strategy = EMACrossVolFilterStrategy(symbol="QQQ", **DEFAULT_KWARGS)

    prepared = strategy.prepare(candles)

    expected_fast = IndicatorEngine(candles).calculate("EMA", period=3)
    expected_slow = IndicatorEngine(candles).calculate("EMA", period=8)
    expected_atr = IndicatorEngine(candles).calculate("ATR", period=5)
    pd.testing.assert_series_equal(prepared["ema_fast"], expected_fast, check_names=False)
    pd.testing.assert_series_equal(prepared["ema_slow"], expected_slow, check_names=False)
    pd.testing.assert_series_equal(prepared["atr"], expected_atr, check_names=False)
    pd.testing.assert_series_equal(
        prepared["atr_pct"], (expected_atr / candles["close"]), check_names=False
    )


def test_prepare_does_not_mutate_input():
    candles = make_candles(VOL_COLLAPSE_EXIT_CLOSES)
    original_columns = list(candles.columns)
    EMACrossVolFilterStrategy(symbol="QQQ", **DEFAULT_KWARGS).prepare(candles)
    assert list(candles.columns) == original_columns


def test_prepare_requires_close_column():
    candles = make_candles(VOL_COLLAPSE_EXIT_CLOSES).drop(columns=["close"])
    with pytest.raises(ValueError):
        EMACrossVolFilterStrategy(symbol="QQQ", **DEFAULT_KWARGS).prepare(candles)


def test_no_signals_when_volatility_never_meets_threshold():
    # A pure gentle-drift series (no burst) -- trend is up throughout,
    # but ATR% never once reaches the (deliberately high) threshold.
    candles = make_candles(_QUIET_WARMUP + _QUIET_DRIFT_UP)
    strategy = EMACrossVolFilterStrategy(symbol="QQQ", fast=3, slow=8, atr_period=5, min_atr_pct=0.5)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    assert signals == []


def test_no_signals_when_trend_never_up():
    # A pure decline -- volatility is elevated by the same day-to-day
    # jumps as the burst scenarios, but the fast EMA never rises above
    # the slow EMA, so no LONG entry condition is ever satisfied.
    candles = make_candles([200 - c for c in _QUIET_WARMUP + _BURST_UP])
    strategy = EMACrossVolFilterStrategy(symbol="QQQ", **DEFAULT_KWARGS)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    assert signals == []


def test_emits_long_when_trend_and_volatility_both_align():
    candles = make_candles(VOL_COLLAPSE_EXIT_CLOSES)
    strategy = EMACrossVolFilterStrategy(symbol="QQQ", **DEFAULT_KWARGS)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    assert len(signals) == 2
    assert signals[0].direction == SignalDirection.LONG
    assert signals[1].direction == SignalDirection.FLAT


def test_exits_on_volatility_collapse_even_while_trend_is_still_up():
    candles = make_candles(VOL_COLLAPSE_EXIT_CLOSES)
    strategy = EMACrossVolFilterStrategy(symbol="QQQ", **DEFAULT_KWARGS)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)
    exit_timestamp = signals[1].timestamp
    exit_row = prepared.loc[exit_timestamp]

    assert signals[1].direction == SignalDirection.FLAT
    # The exit fired specifically because volatility collapsed --
    # the trend condition alone was still satisfied at that bar.
    assert exit_row["ema_fast"] > exit_row["ema_slow"]
    assert exit_row["atr_pct"] < DEFAULT_KWARGS["min_atr_pct"]


def test_exits_on_trend_reversal_even_while_volatility_stays_elevated():
    candles = make_candles(TREND_REVERSAL_EXIT_CLOSES)
    strategy = EMACrossVolFilterStrategy(symbol="QQQ", **DEFAULT_KWARGS)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)
    exit_timestamp = signals[1].timestamp
    exit_row = prepared.loc[exit_timestamp]

    assert len(signals) == 2
    assert signals[0].direction == SignalDirection.LONG
    assert signals[1].direction == SignalDirection.FLAT
    # The exit fired specifically because the trend reversed -- the
    # volatility filter alone was still satisfied at that bar.
    assert exit_row["ema_fast"] <= exit_row["ema_slow"]
    assert exit_row["atr_pct"] >= DEFAULT_KWARGS["min_atr_pct"]


def test_never_emits_short():
    for closes in (VOL_COLLAPSE_EXIT_CLOSES, TREND_REVERSAL_EXIT_CLOSES):
        candles = make_candles(closes)
        strategy = EMACrossVolFilterStrategy(symbol="QQQ", **DEFAULT_KWARGS)
        prepared = strategy.prepare(candles)
        signals = strategy.generate_signals(prepared)
        assert all(s.direction != SignalDirection.SHORT for s in signals)


def test_does_not_re_enter_while_already_long():
    candles = make_candles(VOL_COLLAPSE_EXIT_CLOSES)
    strategy = EMACrossVolFilterStrategy(symbol="QQQ", **DEFAULT_KWARGS)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    long_signals = [s for s in signals if s.direction == SignalDirection.LONG]
    assert len(long_signals) == 1


def test_warmup_period_produces_no_premature_signals():
    # ATR's own warmup (rolling window) and EMA's early bars both
    # produce NaN -- the NaN guard must skip them, not crash.
    candles = make_candles([100, 100.1, 100.2, 100.3])
    strategy = EMACrossVolFilterStrategy(symbol="QQQ", **DEFAULT_KWARGS)
    prepared = strategy.prepare(candles)

    assert pd.isna(prepared["atr_pct"].iloc[0])
    signals = strategy.generate_signals(prepared)  # must not raise
    assert prepared.index[0] not in {s.timestamp for s in signals}


def test_confidence_is_fixed_and_configurable():
    candles = make_candles(VOL_COLLAPSE_EXIT_CLOSES)
    strategy = EMACrossVolFilterStrategy(symbol="QQQ", confidence=0.42, **DEFAULT_KWARGS)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    assert signals  # sanity check
    assert all(s.confidence == 0.42 for s in signals)


def test_signal_metadata_includes_strategy_name():
    candles = make_candles(VOL_COLLAPSE_EXIT_CLOSES)
    strategy = EMACrossVolFilterStrategy(symbol="QQQ", **DEFAULT_KWARGS)
    prepared = strategy.prepare(candles)

    signals = strategy.generate_signals(prepared)

    assert signals
    for signal in signals:
        assert signal.symbol == "QQQ"
        assert signal.metadata["strategy"] == "ema_cross_vol_filter"


def test_params_exposes_constructor_arguments():
    strategy = EMACrossVolFilterStrategy(
        symbol="QQQ", fast=8, slow=21, atr_period=14, min_atr_pct=0.008, confidence=0.6
    )
    assert strategy.params == {
        "fast": 8,
        "slow": 21,
        "atr_period": 14,
        "min_atr_pct": 0.008,
        "confidence": 0.6,
    }


def test_runs_end_to_end_through_backtester_with_default_periods():
    candles = make_candles(VOL_COLLAPSE_EXIT_CLOSES)
    strategy = EMACrossVolFilterStrategy(symbol="QQQ", **DEFAULT_KWARGS)

    result = Backtester().run(strategy, candles)

    assert result.strategy_name == "ema_cross_vol_filter"
    assert isinstance(result.trades, list)
    assert isinstance(result.signals, list)


# ---------------------------------------------------------------------------
# Production-registration proof (Sprint 14 spec, ADR-0047/ADR-0048): once
# registered, a promoted candidate is indistinguishable from any other
# permanent strategy at the registry/reconstruction seam.
# ---------------------------------------------------------------------------


def test_is_discoverable_by_name_through_the_production_strategy_registry():
    assert get_strategy_class("ema_cross_vol_filter") is EMACrossVolFilterStrategy


def test_reconstructs_an_equivalent_instance_from_name_and_params():
    original = EMACrossVolFilterStrategy(
        symbol="QQQ", fast=8, slow=21, atr_period=14, min_atr_pct=0.008, confidence=0.6
    )

    reconstructed = get_strategy_class("ema_cross_vol_filter")(symbol="QQQ", **original.params)

    assert isinstance(reconstructed, EMACrossVolFilterStrategy)
    assert reconstructed.params == original.params
    assert reconstructed.name == original.name
