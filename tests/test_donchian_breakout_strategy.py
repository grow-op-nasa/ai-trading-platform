"""Donchian channel breakout (src/strategies/donchian_breakout.py, Sprint 23, ADR-0061).

Every expected value is worked out by hand from the bars below; nothing is derived by running the strategy.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtesting.engine import Backtester
from src.indicators.engine import IndicatorEngine
from src.signals.models import SignalDirection
from src.strategies.donchian_breakout import DonchianBreakoutStrategy
from src.strategies.history import requirement_of
from src.strategies.registry import available_strategies, get_strategy_class

# close, with high = close + 0.5 and low = close - 0.5
CLOSES = [10, 10, 10, 10, 11, 12, 12, 9, 8, 13]


def candles(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="D", name="timestamp")
    return pd.DataFrame({"open": closes, "high": [c + 0.5 for c in closes], "low": [c - 0.5 for c in closes],
                         "close": closes, "volume": [1000.0] * len(closes)}, index=idx)


def signals_for(closes: list[float], entry: int = 3, exit_: int = 2):
    s = DonchianBreakoutStrategy(symbol="SPY", entry_lookback=entry, exit_lookback=exit_)
    return s.generate_signals(s.prepare(candles(closes)))


def test_channel_uses_prior_bars_only_and_excludes_the_decision_bar():
    c = candles(CLOSES)
    upper = IndicatorEngine(c).calculate("DONCHIAN_UPPER", period=3)
    lower = IndicatorEngine(c).calculate("DONCHIAN_LOWER", period=2)
    assert upper.iloc[:3].isna().all() and pd.notna(upper.iloc[3])
    assert upper.iloc[3] == 10.5          # max(high[0..2]); the bar-3 high is not included
    assert upper.iloc[4] == 10.5          # max(high[1..3]); the breakout bar's own high 11.5 is not included
    assert upper.iloc[7] == 12.5          # max(high[4..6]) = max(11.5, 12.5, 12.5)
    assert lower.iloc[:2].isna().all()
    assert lower.iloc[7] == 11.5          # min(low[5..6]) = min(11.5, 11.5)


def test_entry_exit_and_reentry_at_the_hand_calculated_bars():
    got = [(s.timestamp, s.direction) for s in signals_for(CLOSES)]
    idx = candles(CLOSES).index
    assert got == [(idx[4], SignalDirection.LONG), (idx[7], SignalDirection.FLAT), (idx[9], SignalDirection.LONG)]


def test_no_breakout_means_no_signal():
    assert signals_for([10, 10, 10, 10, 10, 10, 10, 10]) == []


def test_close_equal_to_the_channel_high_is_not_a_breakout():
    closes = list(CLOSES)
    closes[4] = 10.5                       # equals the prior 3-bar high of 10.5
    assert all(s.timestamp != candles(closes).index[4] for s in signals_for(closes))


def test_close_equal_to_the_channel_low_does_not_exit():
    closes = list(CLOSES)
    closes[7] = 11.5                       # equals the prior 2-bar low of 11.5 -> no exit
    idx = candles(closes).index
    got = [s.timestamp for s in signals_for(closes)]
    assert idx[7] not in got and got[0] == idx[4]


def test_state_is_not_repeated_while_the_condition_persists():
    got = signals_for([10, 10, 10, 10, 11, 12, 13, 14, 15, 16])
    assert [s.direction for s in got] == [SignalDirection.LONG]


def test_exit_signal_needs_an_open_position_and_long_only_never_short():
    got = signals_for([20, 19, 18, 17, 16, 15, 14, 13, 12, 11])   # only falls
    assert got == []
    assert all(s.direction is not SignalDirection.SHORT for s in signals_for(CLOSES))


def test_insufficient_history_yields_no_signal():
    assert signals_for([10, 11, 12], entry=3, exit_=2) == []


def test_default_history_requirement_is_the_longest_channel_plus_the_decision_bar():
    lookup = requirement_of(DonchianBreakoutStrategy(symbol="SPY"))
    assert lookup.usable and lookup.requirement.min_bars == 21        # 20 prior bars + the decision bar
    assert requirement_of(DonchianBreakoutStrategy(symbol="SPY", entry_lookback=5, exit_lookback=3)).requirement.min_bars == 6
    assert requirement_of(DonchianBreakoutStrategy(symbol="SPY", entry_lookback=55, exit_lookback=20)).requirement.min_bars == 56


def test_history_requirement_matches_the_first_bar_a_decision_is_possible():
    s = DonchianBreakoutStrategy(symbol="SPY", entry_lookback=3, exit_lookback=2)
    n = s.history_requirement().min_bars
    prepared = s.prepare(candles(CLOSES))
    assert prepared["channel_high"].first_valid_index() == prepared.index[n - 1]


@pytest.mark.parametrize("entry,exit_", [(1, 1), (0, 0), (5, 6), (10, 0), (-3, 2)])
def test_invalid_lookbacks_are_rejected(entry, exit_):
    with pytest.raises(ValueError):
        DonchianBreakoutStrategy(symbol="SPY", entry_lookback=entry, exit_lookback=exit_)


def test_non_integer_lookbacks_are_rejected():
    with pytest.raises(ValueError):
        DonchianBreakoutStrategy(symbol="SPY", entry_lookback=2.5, exit_lookback=1)
    with pytest.raises(ValueError):
        DonchianBreakoutStrategy(symbol="SPY", entry_lookback=True, exit_lookback=1)


def test_signals_never_use_future_bars():
    full = signals_for(CLOSES)
    for cut in range(4, len(CLOSES) + 1):
        prefix = signals_for(CLOSES[:cut])
        expected = [(s.timestamp, s.direction) for s in full if s.timestamp <= candles(CLOSES[:cut]).index[-1]]
        assert [(s.timestamp, s.direction) for s in prefix] == expected


def test_output_is_deterministic():
    a = [(s.timestamp, s.direction, s.confidence) for s in signals_for(CLOSES)]
    b = [(s.timestamp, s.direction, s.confidence) for s in signals_for(CLOSES)]
    assert a == b


def test_does_not_mutate_its_input():
    c = candles(CLOSES)
    before = c.copy()
    s = DonchianBreakoutStrategy(symbol="SPY", entry_lookback=3, exit_lookback=2)
    s.prepare(c)
    pd.testing.assert_frame_equal(c, before)


def test_params_round_trip_and_registration():
    s = DonchianBreakoutStrategy(symbol="QQQ", entry_lookback=30, exit_lookback=15, confidence=0.5)
    assert s.params == {"entry_lookback": 30, "exit_lookback": 15, "confidence": 0.5}
    assert "donchian_breakout" in available_strategies()
    again = get_strategy_class("donchian_breakout")(symbol="QQQ", **s.params)
    assert again.params == s.params and again.name == "donchian_breakout"


def test_signals_carry_symbol_and_strategy_name():
    for sig in signals_for(CLOSES):
        assert sig.symbol == "SPY" and sig.metadata["strategy"] == "donchian_breakout"


def test_runs_through_the_existing_backtester_unchanged():
    s = DonchianBreakoutStrategy(symbol="SPY", entry_lookback=3, exit_lookback=2)
    result = Backtester().run(s, candles(CLOSES))
    assert result.strategy_name == "donchian_breakout"
    assert len(result.trades) == 2 and result.trades[0].entry_time == candles(CLOSES).index[4]
