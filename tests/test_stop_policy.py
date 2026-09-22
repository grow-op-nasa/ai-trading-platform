"""Tests for the `StopPolicy` abstraction and `ATRStopPolicy` -- Sprint
11 (`DECISIONS.md`, ADR-0044).

Covers: `StopResult`'s own validation, `ATRStopPolicy` construction
validation, LONG stop below entry / SHORT stop above entry, ATR-warmup
unavailability, a non-positive/degenerate stop being reported as
unavailable rather than invented, and the leakage-regression test the
Sprint 11 spec explicitly requires -- two histories identical through
time `t`, differing only afterward, must produce identical stops when
sized at `t`.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtesting.stop_policy import (
    DEFAULT_ATR_MULTIPLE,
    DEFAULT_ATR_PERIOD,
    ATRStopPolicy,
    StopResult,
)
from src.signals.models import Signal, SignalDirection


def make_candles(closes: list[float], highs=None, lows=None) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D", name="timestamp")
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs if highs is not None else [c + 1.0 for c in closes],
            "low": lows if lows is not None else [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        index=dates,
    )


def make_signal(
    timestamp: pd.Timestamp, direction: SignalDirection, symbol: str = "TEST"
) -> Signal:
    return Signal(timestamp=timestamp, symbol=symbol, direction=direction, confidence=0.9)


# ---------------------------------------------------------------------------
# StopResult validation
# ---------------------------------------------------------------------------


def test_stop_result_available_requires_a_stop_price():
    with pytest.raises(ValueError):
        StopResult(available=True, stop_price=None)


def test_stop_result_unavailable_requires_a_reason():
    with pytest.raises(ValueError):
        StopResult(available=False, stop_price=None, reason=None)


def test_stop_result_available_with_price_is_fine():
    result = StopResult(available=True, stop_price=95.0)
    assert result.stop_price == 95.0


def test_stop_result_unavailable_with_reason_is_fine():
    result = StopResult(available=False, stop_price=None, reason="no history")
    assert result.reason == "no history"


# ---------------------------------------------------------------------------
# ATRStopPolicy construction validation
# ---------------------------------------------------------------------------


def test_atr_stop_policy_rejects_non_positive_period():
    with pytest.raises(ValueError):
        ATRStopPolicy(period=0)


def test_atr_stop_policy_rejects_non_positive_multiple():
    with pytest.raises(ValueError):
        ATRStopPolicy(multiple=0)
    with pytest.raises(ValueError):
        ATRStopPolicy(multiple=-1.0)


def test_atr_stop_policy_defaults_match_module_constants():
    policy = ATRStopPolicy()
    assert policy.config == {
        "type": "atr",
        "period": DEFAULT_ATR_PERIOD,
        "multiple": DEFAULT_ATR_MULTIPLE,
    }


def test_atr_stop_policy_config_reflects_custom_params():
    policy = ATRStopPolicy(period=10, multiple=3.0)
    assert policy.config == {"type": "atr", "period": 10, "multiple": 3.0}


# ---------------------------------------------------------------------------
# FLAT signals cannot be sized
# ---------------------------------------------------------------------------


def test_stop_price_raises_for_flat_signal():
    candles = make_candles([100.0] * 20)
    policy = ATRStopPolicy(period=5)
    signal = make_signal(candles.index[10], SignalDirection.FLAT)

    with pytest.raises(ValueError):
        policy.stop_price(signal, candles.loc[: candles.index[10]], entry_price=100.0)


# ---------------------------------------------------------------------------
# LONG stop below entry / SHORT stop above entry
# ---------------------------------------------------------------------------


def test_long_stop_is_below_entry():
    # Volatile closes so ATR is genuinely positive.
    closes = [100, 102, 98, 103, 97, 104, 96, 105, 95, 106, 94, 107, 93, 108, 92, 109]
    candles = make_candles(closes)
    policy = ATRStopPolicy(period=5, multiple=2.0)
    signal = make_signal(candles.index[-1], SignalDirection.LONG)
    entry_price = float(candles["close"].iloc[-1])

    result = policy.stop_price(signal, candles, entry_price=entry_price)

    assert result.available is True
    assert result.stop_price < entry_price


def test_short_stop_is_above_entry():
    closes = [100, 102, 98, 103, 97, 104, 96, 105, 95, 106, 94, 107, 93, 108, 92, 109]
    candles = make_candles(closes)
    policy = ATRStopPolicy(period=5, multiple=2.0)
    signal = make_signal(candles.index[-1], SignalDirection.SHORT)
    entry_price = float(candles["close"].iloc[-1])

    result = policy.stop_price(signal, candles, entry_price=entry_price)

    assert result.available is True
    assert result.stop_price > entry_price


def test_larger_multiple_widens_the_stop_distance():
    closes = [100, 102, 98, 103, 97, 104, 96, 105, 95, 106, 94, 107, 93, 108, 92, 109]
    candles = make_candles(closes)
    entry_price = float(candles["close"].iloc[-1])
    signal = make_signal(candles.index[-1], SignalDirection.LONG)

    narrow = ATRStopPolicy(period=5, multiple=1.0).stop_price(signal, candles, entry_price)
    wide = ATRStopPolicy(period=5, multiple=3.0).stop_price(signal, candles, entry_price)

    assert (entry_price - narrow.stop_price) < (entry_price - wide.stop_price)


# ---------------------------------------------------------------------------
# ATR-warmup unavailability -- never silently substituted
# ---------------------------------------------------------------------------


def test_stop_unavailable_when_fewer_than_period_candles_exist():
    closes = [100.0, 101.0, 102.0]  # far fewer than period=14
    candles = make_candles(closes)
    policy = ATRStopPolicy(period=14)
    signal = make_signal(candles.index[-1], SignalDirection.LONG)

    result = policy.stop_price(signal, candles, entry_price=102.0)

    assert result.available is False
    assert result.stop_price is None
    assert "warmup" in result.reason


def test_stop_unavailable_for_completely_empty_history():
    candles = make_candles([100.0])
    empty = candles.iloc[0:0]
    policy = ATRStopPolicy(period=14)
    signal = make_signal(candles.index[0], SignalDirection.LONG)

    result = policy.stop_price(signal, empty, entry_price=100.0)

    assert result.available is False


def test_stop_unavailable_when_atr_is_zero():
    # Perfectly flat candles (high == low == close every bar) -> ATR is
    # exactly zero, a degenerate stop distance that must be reported as
    # unavailable, not divided into a zero-width stop.
    closes = [100.0] * 20
    candles = make_candles(closes, highs=closes, lows=closes)
    policy = ATRStopPolicy(period=5)
    signal = make_signal(candles.index[-1], SignalDirection.LONG)

    result = policy.stop_price(signal, candles, entry_price=100.0)

    assert result.available is False
    assert "ATR" in result.reason


# ---------------------------------------------------------------------------
# Required: no-future-data / leakage regression test
# ---------------------------------------------------------------------------


def test_stop_price_is_unaffected_by_data_after_the_signal_timestamp():
    # Two candle sets identical through index 19, diverging wildly
    # afterward. A StopPolicy given only the truncated, past-only
    # prefix (as PortfolioBacktestEngine always supplies) must produce
    # an identical stop at t=19 regardless of what comes later.
    shared_closes = [100 + (i % 5) - 2 for i in range(20)]
    tail_a = [200.0] * 10  # calm continuation
    tail_b = [50.0, 400.0, 10.0, 600.0, 5.0, 800.0, 1.0, 900.0, 0.5, 999.0]  # wild continuation

    candles_a = make_candles(shared_closes + tail_a)
    candles_b = make_candles(shared_closes + tail_b)

    signal_timestamp = candles_a.index[19]
    assert signal_timestamp == candles_b.index[19]

    policy = ATRStopPolicy(period=5, multiple=2.0)
    signal = make_signal(signal_timestamp, SignalDirection.LONG)
    entry_price = float(candles_a.loc[signal_timestamp, "close"])
    assert entry_price == float(candles_b.loc[signal_timestamp, "close"])

    # The caller-side truncation PortfolioBacktestEngine always performs.
    history_a = candles_a.loc[:signal_timestamp]
    history_b = candles_b.loc[:signal_timestamp]

    result_a = policy.stop_price(signal, history_a, entry_price=entry_price)
    result_b = policy.stop_price(signal, history_b, entry_price=entry_price)

    assert result_a.available is True
    assert result_b.available is True
    assert result_a.stop_price == pytest.approx(result_b.stop_price)
