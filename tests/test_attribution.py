"""Tests for Performance Attribution (src/attribution).

Regime ground truth is computed directly via `MarketRegimeEngine` in
each test rather than hardcoded, so these tests check that
`PerformanceAttributor` correctly looks up and combines the engine's
own output -- not a restatement of the regime engine's math (already
covered by `tests/test_regime.py`).
"""

from __future__ import annotations

from uuid import uuid4

import pandas as pd
import pytest

from src.attribution.engine import PerformanceAttributor
from src.backtesting.models import BacktestResult, Trade
from src.regime.engine import MarketRegimeEngine

# Short warmup windows so small synthetic candle sets clear them.
REGIME_KWARGS = dict(
    trend_fast=3, trend_slow=6, volatility_period=3, volatility_lookback=10
)


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


def make_trade(candles: pd.DataFrame, entry_idx: int, exit_idx: int, pnl: float) -> Trade:
    entry_time = candles.index[entry_idx]
    exit_time = candles.index[exit_idx]
    entry_price = float(candles.loc[entry_time, "close"])
    return Trade(
        entry_time=entry_time,
        exit_time=exit_time,
        direction=1,
        entry_price=entry_price,
        exit_price=entry_price + pnl,
        entry_signal_id=uuid4(),
    )


def make_result(trades: list[Trade]) -> BacktestResult:
    return BacktestResult(
        strategy_name="test", trades=trades, equity_curve=pd.Series(dtype=float), metrics={}
    )


def test_empty_trades_returns_neutral_report():
    candles = make_candles([100.0] * 10)
    result = make_result([])

    report = PerformanceAttributor().run(result, candles)

    assert report.total_trades == 0
    assert report.winning_trades == 0
    assert report.win_rate is None
    assert report.average_hold is None
    assert report.regime_breakdown == {}
    assert report.best_regime is None
    assert report.worst_regime is None


def test_counts_and_win_rate():
    candles = make_candles([100.0] * 30)
    trades = [
        make_trade(candles, 5, 10, pnl=10),  # win
        make_trade(candles, 15, 20, pnl=-10),  # loss
    ]
    result = make_result(trades)

    report = PerformanceAttributor().run(result, candles, **REGIME_KWARGS)

    assert report.total_trades == 2
    assert report.winning_trades == 1
    assert report.win_rate == pytest.approx(0.5)


def test_average_hold_is_mean_of_exit_minus_entry():
    candles = make_candles([100.0] * 30)
    trades = [
        make_trade(candles, 0, 5, pnl=1),   # 5-day hold
        make_trade(candles, 10, 25, pnl=1),  # 15-day hold
    ]
    result = make_result(trades)

    report = PerformanceAttributor().run(result, candles, **REGIME_KWARGS)

    assert report.average_hold == pd.Timedelta(days=10)


def test_trade_during_warmup_is_bucketed_unknown_and_excluded_from_best_worst():
    candles = make_candles([100 + i for i in range(30)])
    # index 1 can't have a real SMA(6)-based trend score yet.
    trade = make_trade(candles, entry_idx=1, exit_idx=3, pnl=1)
    result = make_result([trade])

    report = PerformanceAttributor().run(result, candles, **REGIME_KWARGS)

    assert "unknown" in report.regime_breakdown
    assert report.regime_breakdown["unknown"].label == "Unknown"
    assert report.best_regime is None
    assert report.worst_regime is None


def test_regime_label_matches_engine_dominant_at_entry_time():
    candles = make_candles([100 + i for i in range(30)])
    entry_idx = 20
    trade = make_trade(candles, entry_idx=entry_idx, exit_idx=25, pnl=1)
    result = make_result([trade])

    report = PerformanceAttributor().run(result, candles, **REGIME_KWARGS)

    engine = MarketRegimeEngine(candles)
    expected_row = engine.dominant(**REGIME_KWARGS).loc[candles.index[entry_idx]]
    expected_label = (
        f"{expected_row['trend_regime'].title()} + "
        f"{expected_row['volatility_regime'].replace('_', ' ').title()}"
    )

    assert len(report.regime_breakdown) == 1
    bucket = next(iter(report.regime_breakdown.values()))
    assert bucket.label == expected_label
    assert bucket.trade_count == 1
    assert report.best_regime == expected_label
    assert report.worst_regime == expected_label


def test_best_and_worst_regime_reflect_trade_returns_across_two_buckets():
    flat = [100.0] * 20
    trend = [100 + i * 3 for i in range(1, 21)]
    candles = make_candles(flat + trend)

    engine = MarketRegimeEngine(candles)
    dominant = engine.dominant(**REGIME_KWARGS)
    flat_row = dominant.loc[candles.index[15]]
    trend_row = dominant.loc[candles.index[35]]
    # Sanity check on the test's own construction: the two entry points
    # must actually land in different regime buckets, or this test
    # isn't exercising what it claims to.
    assert (flat_row["trend_regime"], flat_row["volatility_regime"]) != (
        trend_row["trend_regime"],
        trend_row["volatility_regime"],
    )

    winning_trade = make_trade(candles, entry_idx=35, exit_idx=36, pnl=10)
    losing_trade = make_trade(candles, entry_idx=15, exit_idx=16, pnl=-10)
    result = make_result([winning_trade, losing_trade])

    report = PerformanceAttributor().run(result, candles, **REGIME_KWARGS)

    def expected_label(row: pd.Series) -> str:
        return (
            f"{row['trend_regime'].title()} + "
            f"{row['volatility_regime'].replace('_', ' ').title()}"
        )

    assert report.best_regime == expected_label(trend_row)
    assert report.worst_regime == expected_label(flat_row)


def test_report_string_contains_key_fields():
    candles = make_candles([100.0] * 30)
    trade = make_trade(candles, 5, 10, pnl=5)
    result = make_result([trade])

    report = PerformanceAttributor().run(result, candles, **REGIME_KWARGS)
    text = report.report()

    assert "Trades: 1" in text
    assert "Winning Trades: 1" in text
    assert "Win Rate" in text
    assert "Average Hold" in text


def test_average_hold_formatting_uses_minutes_for_short_holds():
    dates = pd.date_range("2024-01-01", periods=10, freq="5min", name="timestamp")
    candles = pd.DataFrame(
        {
            "open": [100.0] * 10,
            "high": [100.1] * 10,
            "low": [99.9] * 10,
            "close": [100.0] * 10,
            "volume": [1000.0] * 10,
        },
        index=dates,
    )
    trade = Trade(
        entry_time=dates[0],
        exit_time=dates[4],  # 20 minutes later
        direction=1,
        entry_price=100.0,
        exit_price=101.0,
        entry_signal_id=uuid4(),
    )
    result = make_result([trade])

    report = PerformanceAttributor().run(result, candles, **REGIME_KWARGS)

    assert "min" in report.report()
