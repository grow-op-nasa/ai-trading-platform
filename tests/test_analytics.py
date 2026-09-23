"""Tests for `src.analytics` -- Sprint 9 (`DECISIONS.md`, ADR-0042).

Deliberately hand-calculable: every fixture's expected metric value is
computed the same way a human checking the platform's math would, not
derived from the implementation itself. Covers the specific edge cases
the Sprint 9 spec calls out by name (empty trades, one trade, all
winning, all losing, mixed, zero P&L, zero variance, missing equity
observations, a flat equity curve) plus the timeframe/annualization and
experiment-comparison behavior. `src.analytics.valuation` has its own
file, `tests/test_portfolio_valuation.py` -- a genuinely separate
concern (market-data boundary, read-only Portfolio mutation) from the
pure metric math here.

Entirely network-free (Sprint 9 spec, section 33): every fixture is
either a hand-built `Trade`/`pd.Series`, or a synthetic candle
DataFrame run through the real `Backtester`/`EMACrossStrategy` --
nothing here touches `MarketDataService` or a real provider.
"""

from __future__ import annotations

from uuid import uuid4

import pandas as pd
import pytest

from src.analytics import metrics as m
from src.analytics.models import (
    BacktestAnalytics,
    ComparisonResult,
    ComparisonWarning,
    Metric,
    MetricStatus,
)
from src.analytics.service import AnalyticsService, compare_experiments
from src.backtesting.models import BacktestResult, Trade
from src.data.base import Interval
from src.experiments.registry import ExperimentRegistry
from src.strategies.ema_cross import EMACrossStrategy
from src.backtesting.engine import Backtester

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_trade(
    entry_price: float,
    exit_price: float,
    direction: int = 1,
    entry_time: pd.Timestamp | None = None,
    exit_time: pd.Timestamp | None = None,
    quantity: float | None = None,
    entry_fill_price: float | None = None,
    exit_fill_price: float | None = None,
    entry_fee: float = 0.0,
    exit_fee: float = 0.0,
) -> Trade:
    return Trade(
        entry_time=entry_time or pd.Timestamp("2024-01-01", tz="UTC"),
        exit_time=exit_time or pd.Timestamp("2024-01-02", tz="UTC"),
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        entry_signal_id=uuid4(),
        quantity=quantity,
        entry_fill_price=entry_fill_price,
        exit_fill_price=exit_fill_price,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
    )


def make_curve(values: list[float], freq: str = "D") -> pd.Series:
    index = pd.date_range("2024-01-01", periods=len(values), freq=freq, tz="UTC", name="timestamp")
    return pd.Series(values, index=index, name="equity")


def assert_undefined(metric: Metric, reason_contains: str | None = None) -> None:
    assert metric.status is MetricStatus.UNDEFINED
    assert metric.value is None
    if reason_contains is not None:
        assert reason_contains in metric.reason


def assert_ok(metric: Metric, expected: float) -> None:
    assert metric.status is MetricStatus.OK
    assert metric.reason is None
    assert metric.value == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Metric / MetricStatus (models.py)
# ---------------------------------------------------------------------------


def test_metric_of_is_ok_with_no_reason():
    metric = Metric.of(1.5)
    assert metric.status is MetricStatus.OK
    assert metric.value == 1.5
    assert metric.reason is None
    assert metric.is_defined is True


def test_metric_undefined_has_no_value():
    metric = Metric.undefined("no data")
    assert metric.status is MetricStatus.UNDEFINED
    assert metric.value is None
    assert metric.reason == "no data"
    assert metric.is_defined is False


# ---------------------------------------------------------------------------
# win_rate / trade_count / winning_trade_count / losing_trade_count
# ---------------------------------------------------------------------------


def test_win_rate_undefined_for_no_trades():
    assert_undefined(m.win_rate([]), "no closed trades")


def test_win_rate_one_trade_win():
    assert_ok(m.win_rate([make_trade(100, 110)]), 1.0)


def test_win_rate_one_trade_loss():
    assert_ok(m.win_rate([make_trade(100, 90)]), 0.0)


def test_win_rate_all_winning():
    trades = [make_trade(100, 110), make_trade(100, 105), make_trade(100, 120)]
    assert_ok(m.win_rate(trades), 1.0)


def test_win_rate_all_losing():
    trades = [make_trade(100, 90), make_trade(100, 95)]
    assert_ok(m.win_rate(trades), 0.0)


def test_win_rate_mixed():
    trades = [make_trade(100, 110), make_trade(100, 90), make_trade(100, 105)]
    assert_ok(m.win_rate(trades), 2 / 3)


def test_win_rate_zero_pnl_trade_does_not_count_as_a_win():
    # A trade that closes at exactly its entry price counts toward the
    # denominator (it's a closed trade) but not the numerator (it isn't
    # a *winning* trade) -- "winning" is strictly return_pct > 0.
    trades = [make_trade(100, 100), make_trade(100, 110)]
    assert_ok(m.win_rate(trades), 0.5)


def test_trade_count_zero_is_a_defined_fact_not_undefined():
    assert m.trade_count([]) == 0


def test_trade_count_normal():
    assert m.trade_count([make_trade(100, 110), make_trade(100, 90)]) == 2


def test_winning_and_losing_trade_counts():
    trades = [make_trade(100, 110), make_trade(100, 90), make_trade(100, 100)]
    assert m.winning_trade_count(trades) == 1
    assert m.losing_trade_count(trades) == 1


# ---------------------------------------------------------------------------
# profit_factor
# ---------------------------------------------------------------------------


def test_profit_factor_undefined_for_no_trades():
    assert_undefined(m.profit_factor([]), "no closed trades")


def test_profit_factor_undefined_when_no_losing_trades():
    # Sprint 9 spec, section 6: must not silently produce infinity.
    trades = [make_trade(100, 110), make_trade(100, 105)]
    assert_undefined(m.profit_factor(trades), "no losing trades")


def test_profit_factor_all_losing_is_zero_not_undefined():
    # Gross profit is a real, known 0 here -- 0 / gross_loss is defined.
    trades = [make_trade(100, 90), make_trade(100, 95)]
    assert_ok(m.profit_factor(trades), 0.0)


def test_profit_factor_mixed_hand_calculated():
    # Winner: +10% (0.10). Loser: -5% (0.05). Winner: +20% (0.20).
    trades = [make_trade(100, 110), make_trade(100, 95), make_trade(100, 120)]
    gross_profit = 0.10 + 0.20
    gross_loss = 0.05
    assert_ok(m.profit_factor(trades), gross_profit / gross_loss)


# ---------------------------------------------------------------------------
# expectancy / average / largest winner+loser
# ---------------------------------------------------------------------------


def test_expectancy_undefined_for_no_trades():
    assert_undefined(m.expectancy([]), "no closed trades")


def test_expectancy_hand_calculated():
    trades = [make_trade(100, 110), make_trade(100, 90)]  # +0.10, -0.10
    assert_ok(m.expectancy(trades), 0.0)


def test_average_winner_undefined_when_no_winners():
    assert_undefined(m.average_winner([make_trade(100, 90)]), "no winning trades")


def test_average_loser_undefined_when_no_losers():
    assert_undefined(m.average_loser([make_trade(100, 110)]), "no losing trades")


def test_average_winner_and_loser_hand_calculated():
    trades = [make_trade(100, 110), make_trade(100, 130), make_trade(100, 90)]
    assert_ok(m.average_winner(trades), (0.10 + 0.30) / 2)
    assert_ok(m.average_loser(trades), -0.10)


def test_largest_winner_and_loser_undefined_cases():
    assert_undefined(m.largest_winner([make_trade(100, 90)]), "no winning trades")
    assert_undefined(m.largest_loser([make_trade(100, 110)]), "no losing trades")


def test_largest_winner_and_loser_hand_calculated():
    trades = [make_trade(100, 110), make_trade(100, 130), make_trade(100, 80), make_trade(100, 95)]
    assert_ok(m.largest_winner(trades), 0.30)
    assert_ok(m.largest_loser(trades), -0.20)


# ---------------------------------------------------------------------------
# Sprint 11 (DECISIONS.md, ADR-0044): dollar-denominated per-trade
# metrics, gated on Trade.quantity being populated for every trade
# (a RiskMode.PORTFOLIO_RISK result) -- the fraction-based originals
# above stay exactly as they were for every LEGACY_UNIT trade list.
# ---------------------------------------------------------------------------


def test_has_quantity_detail_false_for_empty_and_legacy_trades():
    assert m.has_quantity_detail([]) is False
    assert m.has_quantity_detail([make_trade(100, 110)]) is False  # quantity=None


def test_has_quantity_detail_true_when_every_trade_is_sized():
    trades = [make_trade(100, 110, quantity=10.0), make_trade(100, 90, quantity=5.0)]
    assert m.has_quantity_detail(trades) is True


def test_has_quantity_detail_false_for_a_mixed_list():
    trades = [make_trade(100, 110, quantity=10.0), make_trade(100, 90)]
    assert m.has_quantity_detail(trades) is False


def test_dollar_metrics_undefined_for_legacy_unit_trades():
    trades = [make_trade(100, 110), make_trade(100, 90)]
    for fn in (
        m.net_pnl_dollars,
        m.gross_profit_dollars,
        m.gross_loss_dollars,
        m.expectancy_dollars,
        m.average_winner_dollars,
        m.average_loser_dollars,
        m.largest_winner_dollars,
        m.largest_loser_dollars,
    ):
        assert_undefined(fn(trades), "no quantity detail")


def test_dollar_metrics_undefined_for_no_trades_at_all():
    assert_undefined(m.net_pnl_dollars([]), "no quantity detail")


def test_net_pnl_dollars_hand_calculated():
    # LONG 100->110 qty 20: +200. SHORT 100->110 qty 10: -100. Net: +100.
    trades = [
        make_trade(100, 110, direction=1, quantity=20.0),
        make_trade(100, 110, direction=-1, quantity=10.0),
    ]
    assert_ok(m.net_pnl_dollars(trades), 100.0)


def test_gross_profit_and_loss_dollars_hand_calculated():
    trades = [
        make_trade(100, 110, quantity=20.0),  # +200
        make_trade(100, 130, quantity=10.0),  # +300
        make_trade(100, 90, quantity=15.0),  # -150
    ]
    assert_ok(m.gross_profit_dollars(trades), 500.0)
    assert_ok(m.gross_loss_dollars(trades), 150.0)


def test_expectancy_dollars_hand_calculated():
    trades = [make_trade(100, 110, quantity=20.0), make_trade(100, 90, quantity=20.0)]
    # +200 and -200 -> average 0.0
    assert_ok(m.expectancy_dollars(trades), 0.0)


def test_average_winner_and_loser_dollars_hand_calculated():
    trades = [
        make_trade(100, 110, quantity=20.0),  # +200
        make_trade(100, 130, quantity=10.0),  # +300
        make_trade(100, 90, quantity=15.0),  # -150
    ]
    assert_ok(m.average_winner_dollars(trades), (200.0 + 300.0) / 2)
    assert_ok(m.average_loser_dollars(trades), -150.0)


def test_average_winner_dollars_undefined_when_no_winners():
    assert_undefined(m.average_winner_dollars([make_trade(100, 90, quantity=10.0)]), "no winning trades")


def test_average_loser_dollars_undefined_when_no_losers():
    assert_undefined(m.average_loser_dollars([make_trade(100, 110, quantity=10.0)]), "no losing trades")


def test_largest_winner_and_loser_dollars_hand_calculated():
    trades = [
        make_trade(100, 110, quantity=20.0),  # +200
        make_trade(100, 130, quantity=10.0),  # +300
        make_trade(100, 80, quantity=5.0),  # -100
        make_trade(100, 95, quantity=30.0),  # -150
    ]
    assert_ok(m.largest_winner_dollars(trades), 300.0)
    assert_ok(m.largest_loser_dollars(trades), -150.0)


def test_analytics_service_populates_dollar_metrics_when_quantity_present():
    trades = [make_trade(100, 110, quantity=20.0), make_trade(100, 90, quantity=20.0)]
    curve = make_curve([100_000.0, 100_200.0, 99_800.0])
    analytics = AnalyticsService()._analyze(
        trades=trades, equity_curve=curve, spec=None, experiment_id=None
    )
    assert analytics.has_quantity_detail is True
    assert analytics.net_pnl_dollars.value == pytest.approx(0.0)
    # The fraction-based fields remain populated too -- additive, not replaced.
    assert analytics.expectancy.status is MetricStatus.OK


def test_analytics_service_dollar_metrics_undefined_for_legacy_trades():
    trades = [make_trade(100, 110), make_trade(100, 90)]
    curve = make_curve([100_000.0, 100_200.0, 99_800.0])
    analytics = AnalyticsService()._analyze(
        trades=trades, equity_curve=curve, spec=None, experiment_id=None
    )
    assert analytics.has_quantity_detail is False
    assert analytics.net_pnl_dollars.status is MetricStatus.UNDEFINED
    # Fraction-based expectancy still works for a LEGACY_UNIT trade list.
    assert analytics.expectancy.status is MetricStatus.OK


# ---------------------------------------------------------------------------
# Execution-cost metrics (Sprint 12, `DECISIONS.md` ADR-0045) --
# has_execution_cost_detail / total_fees_dollars / total_slippage_cost_dollars
# / net_pnl_after_costs_dollars. The dollar metrics above (net_pnl_dollars,
# etc.) are computed from the frictionless reference price and remain
# unaffected by any of this -- these are additive, cost-aware metrics.
# ---------------------------------------------------------------------------


def test_has_execution_cost_detail_false_for_empty_and_frictionless_trades():
    assert m.has_execution_cost_detail([]) is False
    # quantity present but no fill prices at all (a zero-cost/legacy PORTFOLIO_RISK run).
    assert m.has_execution_cost_detail([make_trade(100, 110, quantity=10.0)]) is False


def test_has_execution_cost_detail_true_when_every_trade_has_both_fill_prices():
    trades = [
        make_trade(100, 110, quantity=10.0, entry_fill_price=100.5, exit_fill_price=109.5),
        make_trade(100, 90, quantity=5.0, entry_fill_price=100.2, exit_fill_price=89.8),
    ]
    assert m.has_execution_cost_detail(trades) is True


def test_has_execution_cost_detail_false_for_a_mixed_list():
    trades = [
        make_trade(100, 110, quantity=10.0, entry_fill_price=100.5, exit_fill_price=109.5),
        make_trade(100, 90, quantity=5.0),  # no fill prices -- e.g. a synthesized end-of-data close
    ]
    assert m.has_execution_cost_detail(trades) is False


def test_total_fees_dollars_only_needs_quantity_detail_not_fill_prices():
    # Fees default to 0.0 per trade regardless of fill-price detail --
    # total_fees_dollars is defined as soon as quantity detail exists.
    trades = [make_trade(100, 110, quantity=10.0, entry_fee=1.0, exit_fee=2.0)]
    assert_ok(m.total_fees_dollars(trades), 3.0)


def test_total_fees_dollars_undefined_for_legacy_unit_trades():
    assert_undefined(m.total_fees_dollars([make_trade(100, 110)]), "no quantity detail")


def test_total_fees_dollars_sums_across_multiple_trades():
    trades = [
        make_trade(100, 110, quantity=10.0, entry_fee=1.0, exit_fee=1.0),
        make_trade(100, 90, quantity=5.0, entry_fee=0.5, exit_fee=0.5),
    ]
    assert_ok(m.total_fees_dollars(trades), 3.0)


def test_total_slippage_cost_dollars_undefined_without_fill_price_detail():
    assert_undefined(
        m.total_slippage_cost_dollars([make_trade(100, 110, quantity=10.0)]),
        "no execution fill-price detail",
    )


def test_total_slippage_cost_dollars_hand_calculated():
    # LONG 100->110, qty 10, reference gross = 100. Actual fills:
    # entry 100.5 (paid more), exit 109.5 (received less) -- fill P&L =
    # 10*(109.5-100.5) = 90.0. slippage_cost = gross(100) - fill(90) = 10.0.
    trades = [make_trade(100, 110, quantity=10.0, entry_fill_price=100.5, exit_fill_price=109.5)]
    assert_ok(m.total_slippage_cost_dollars(trades), 10.0)


def test_net_pnl_after_costs_dollars_hand_calculated():
    # Same trade as above, plus $2 total fees:
    # gross = 100.0, slippage_cost = 10.0, fees = 2.0 -> net = 88.0.
    trades = [
        make_trade(
            100, 110, quantity=10.0,
            entry_fill_price=100.5, exit_fill_price=109.5,
            entry_fee=1.0, exit_fee=1.0,
        )
    ]
    assert_ok(m.net_pnl_after_costs_dollars(trades), 88.0)


def test_net_pnl_after_costs_dollars_undefined_without_fill_price_detail():
    assert_undefined(
        m.net_pnl_after_costs_dollars([make_trade(100, 110, quantity=10.0)]),
        "no execution fill-price detail",
    )


def test_analytics_service_populates_execution_cost_metrics_when_fills_present():
    trades = [
        make_trade(
            100, 110, quantity=20.0,
            entry_fill_price=100.5, exit_fill_price=109.0,
            entry_fee=1.0, exit_fee=1.0,
        )
    ]
    curve = make_curve([100_000.0, 100_200.0, 99_800.0])
    analytics = AnalyticsService()._analyze(
        trades=trades, equity_curve=curve, spec=None, experiment_id=None
    )
    assert analytics.has_execution_cost_detail is True
    assert analytics.total_fees_dollars.value == pytest.approx(2.0)
    assert analytics.total_slippage_cost_dollars.status is MetricStatus.OK
    assert analytics.net_pnl_after_costs_dollars.status is MetricStatus.OK
    # The dollar P&L computed from the reference price stays populated too.
    assert analytics.net_pnl_dollars.status is MetricStatus.OK


def test_analytics_service_execution_cost_metrics_undefined_without_fills():
    trades = [make_trade(100, 110, quantity=20.0)]
    curve = make_curve([100_000.0, 100_200.0])
    analytics = AnalyticsService()._analyze(
        trades=trades, equity_curve=curve, spec=None, experiment_id=None
    )
    assert analytics.has_execution_cost_detail is False
    assert analytics.total_slippage_cost_dollars.status is MetricStatus.UNDEFINED
    assert analytics.net_pnl_after_costs_dollars.status is MetricStatus.UNDEFINED
    # Fees remain defined regardless (quantity detail alone is enough).
    assert analytics.total_fees_dollars.status is MetricStatus.OK


# ---------------------------------------------------------------------------
# total_pnl / total_return (equity-curve based, dollar/fractional totals)
# ---------------------------------------------------------------------------


def test_total_pnl_undefined_for_empty_curve():
    assert_undefined(m.total_pnl(pd.Series(dtype=float)), "no equity observations")


def test_total_pnl_single_observation_is_zero_not_undefined():
    assert_ok(m.total_pnl(make_curve([100_000.0])), 0.0)


def test_total_pnl_hand_calculated():
    assert_ok(m.total_pnl(make_curve([100_000.0, 105_000.0, 98_000.0])), -2_000.0)


def test_total_return_undefined_for_empty_curve():
    assert_undefined(m.total_return(pd.Series(dtype=float)), "no equity observations")


def test_total_return_undefined_when_curve_starts_at_zero():
    assert_undefined(m.total_return(make_curve([0.0, 50.0])), "starts at zero")


def test_total_return_hand_calculated():
    assert_ok(m.total_return(make_curve([100.0, 150.0])), 0.5)


# ---------------------------------------------------------------------------
# max_drawdown -- equity-curve based, never approximated from final P&L
# ---------------------------------------------------------------------------


def test_max_drawdown_undefined_for_empty_curve():
    assert_undefined(m.max_drawdown(pd.Series(dtype=float)), "no equity observations")


def test_max_drawdown_flat_curve_is_zero_not_undefined():
    assert_ok(m.max_drawdown(make_curve([100.0, 100.0, 100.0])), 0.0)


def test_max_drawdown_hand_calculated():
    # Running peak: 100, 120, 120, 120. Drawdown: 0, 0, -0.25, -1/12.
    assert_ok(m.max_drawdown(make_curve([100.0, 120.0, 90.0, 110.0])), -0.25)


def test_max_drawdown_ignores_final_value_alone():
    # A curve that ends near its starting value but dipped hard in the
    # middle must report the dip, not "ended flat, so drawdown ~0".
    curve = make_curve([100.0, 50.0, 100.0])
    assert_ok(m.max_drawdown(curve), -0.5)


def test_drawdown_curve_empty_for_empty_equity_curve():
    result = m.drawdown_curve(pd.Series(dtype=float))
    assert result.empty


def test_drawdown_curve_hand_calculated():
    curve = make_curve([100.0, 120.0, 90.0, 110.0])
    result = m.drawdown_curve(curve)
    expected = [0.0, 0.0, -0.25, -1 / 12]
    assert list(result.values) == pytest.approx(expected)


def test_drawdown_curve_min_matches_max_drawdown():
    curve = make_curve([100.0, 120.0, 90.0, 110.0, 95.0])
    assert m.drawdown_curve(curve).min() == pytest.approx(m.max_drawdown(curve).value)


# ---------------------------------------------------------------------------
# sharpe_ratio -- periodic returns, explicit timeframe-aware annualization
# ---------------------------------------------------------------------------


def test_sharpe_undefined_for_fewer_than_two_period_returns():
    result = m.sharpe_ratio(make_curve([100_000.0]))
    assert_undefined(result.metric, "fewer than 2")
    assert result.periods_per_year is None


def test_sharpe_undefined_for_zero_variance():
    result = m.sharpe_ratio(make_curve([100.0, 101.0, 102.01]))  # constant +1% each step
    assert_undefined(result.metric, "zero variance")
    assert result.periods_per_year is None


def test_sharpe_hand_calculated_daily():
    curve = make_curve([100.0, 110.0, 99.0, 108.9])
    returns = curve.pct_change().dropna()
    std = returns.std(ddof=0)
    expected = (returns.mean() / std) * (252**0.5)
    result = m.sharpe_ratio(curve)
    assert result.periods_per_year == 252
    assert_ok(result.metric, expected)


def test_sharpe_explicit_periods_per_year_overrides_inference():
    curve = make_curve([100.0, 110.0, 99.0, 108.9])
    result = m.sharpe_ratio(curve, periods_per_year=12)
    assert result.periods_per_year == 12


def test_sharpe_annualization_differs_between_daily_and_intraday_bars():
    # ADR-0038: never a blind sqrt(252) regardless of timeframe. Same
    # relative return pattern, two different bar spacings -> two
    # different (and correct) annualization factors.
    values = [100.0, 110.0, 99.0, 108.9, 100.0]
    daily = m.sharpe_ratio(make_curve(values, freq="D"))
    minute = m.sharpe_ratio(make_curve(values, freq="min"))
    assert daily.periods_per_year == 252
    assert minute.periods_per_year == 252 * 24 * 60
    assert daily.metric.value != pytest.approx(minute.metric.value)


# ---------------------------------------------------------------------------
# volatility -- same annualization convention as sharpe_ratio
# ---------------------------------------------------------------------------


def test_volatility_undefined_for_fewer_than_two_period_returns():
    result = m.volatility(make_curve([100.0]))
    assert_undefined(result.metric, "fewer than 2")


def test_volatility_hand_calculated():
    curve = make_curve([100.0, 110.0, 99.0, 108.9])
    returns = curve.pct_change().dropna()
    std = returns.std(ddof=0)
    expected = std * (252**0.5)
    result = m.volatility(curve)
    assert result.periods_per_year == 252
    assert_ok(result.metric, expected)


def test_volatility_and_sharpe_use_the_same_periods_per_year():
    curve = make_curve([100.0, 110.0, 99.0, 108.9, 103.0], freq="min")
    sharpe = m.sharpe_ratio(curve)
    vol = m.volatility(curve)
    assert sharpe.periods_per_year == vol.periods_per_year


# ---------------------------------------------------------------------------
# exposure_time
# ---------------------------------------------------------------------------


def test_exposure_time_undefined_for_empty_or_single_point_curve():
    assert_undefined(m.exposure_time([], pd.Series(dtype=float)), "no elapsed time")
    assert_undefined(m.exposure_time([], make_curve([100.0])), "no elapsed time")


def test_exposure_time_zero_for_no_trades():
    assert_ok(m.exposure_time([], make_curve([100.0, 101.0, 102.0])), 0.0)


def test_exposure_time_hand_calculated():
    curve = make_curve([100.0] * 10)  # 10 daily points -> 9 days elapsed
    trade = make_trade(
        100,
        110,
        entry_time=pd.Timestamp("2024-01-03", tz="UTC"),
        exit_time=pd.Timestamp("2024-01-06", tz="UTC"),  # 3 days held
    )
    assert_ok(m.exposure_time([trade], curve), 3 / 9)


def test_exposure_time_clips_a_trade_extending_past_the_curve_end():
    curve = make_curve([100.0] * 5)  # 4 days elapsed (Jan 1 -> Jan 5)
    trade = make_trade(
        100,
        110,
        entry_time=pd.Timestamp("2024-01-04", tz="UTC"),
        exit_time=pd.Timestamp("2024-01-20", tz="UTC"),  # exits long after curve ends
    )
    result = m.exposure_time([trade], curve)
    assert result.value <= 1.0


# ---------------------------------------------------------------------------
# AnalyticsService.analyze_backtest
# ---------------------------------------------------------------------------


def _run_ema_backtest() -> BacktestResult:
    closes = [110, 108, 106, 104, 102, 100, 105, 110, 115, 120, 110, 100, 90, 80]
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D", name="timestamp")
    candles = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        index=dates,
    )
    strategy = EMACrossStrategy(symbol="SPY", fast=2, slow=4)
    return Backtester().run(strategy, candles)


def test_analyze_backtest_without_spec_leaves_identity_fields_none():
    result = _run_ema_backtest()
    analytics = AnalyticsService().analyze_backtest(result)
    assert isinstance(analytics, BacktestAnalytics)
    assert analytics.experiment_id is None
    assert analytics.strategy_name is None
    assert analytics.symbol is None
    assert analytics.has_trade_detail is True
    assert analytics.has_equity_curve is True


def test_analyze_backtest_with_spec_carries_identity(tmp_path):
    from src.experiments.spec import ExperimentSpec
    from src.risk.models import RiskLimits

    result = _run_ema_backtest()
    closes = [110, 108, 106, 104, 102, 100, 105, 110, 115, 120, 110, 100, 90, 80]
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D", name="timestamp")
    candles = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1.0] * len(closes)},
        index=dates,
    )
    strategy = EMACrossStrategy(symbol="SPY", fast=2, slow=4)
    spec = ExperimentSpec.capture(strategy, candles, RiskLimits(), symbol="SPY", interval="1d", dataset_source="test")

    analytics = AnalyticsService().analyze_backtest(result, spec=spec, experiment_id=7)
    assert analytics.experiment_id == 7
    assert analytics.strategy_name == "ema_cross"
    assert analytics.symbol == "SPY"
    assert analytics.interval == Interval.DAY_1
    assert analytics.dataset_fingerprint == spec.dataset_fingerprint


def test_analyze_backtest_empty_result_is_fully_undefined_but_valid():
    empty = BacktestResult(strategy_name="x", trades=[], equity_curve=pd.Series(dtype=float), metrics={}, signals=[])
    analytics = AnalyticsService().analyze_backtest(empty)
    assert analytics.total_pnl.status is MetricStatus.UNDEFINED
    assert analytics.win_rate.status is MetricStatus.UNDEFINED
    assert analytics.trade_count == 0
    assert analytics.has_trade_detail is False
    assert analytics.has_equity_curve is False


# ---------------------------------------------------------------------------
# AnalyticsService.analyze_experiment (registry-backed)
# ---------------------------------------------------------------------------


def _make_registry(tmp_path) -> ExperimentRegistry:
    return ExperimentRegistry(db_path=tmp_path / "experiments.db")


def _run_and_persist(registry, symbol="SPY", strategy="ema_cross", params=None):
    from scripts.run_experiment import run_experiment

    closes = [110, 108, 106, 104, 102, 100, 105, 110, 115, 120, 110, 100, 90, 80]
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D", name="timestamp")
    candles = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1.0] * len(closes)},
        index=dates,
    )
    return run_experiment(
        strategy_name=strategy, symbol=symbol, candles=candles,
        strategy_params=params or {"fast": 2, "slow": 4}, registry=registry,
    )


def test_analyze_experiment_returns_none_for_unknown_id(tmp_path):
    registry = _make_registry(tmp_path)
    assert AnalyticsService().analyze_experiment(registry, 999) is None


def test_analyze_experiment_loads_persisted_trades_and_equity(tmp_path):
    registry = _make_registry(tmp_path)
    run_result = _run_and_persist(registry)

    analytics = AnalyticsService().analyze_experiment(registry, run_result.experiment_id)
    assert analytics is not None
    assert analytics.strategy_name == "ema_cross"
    assert analytics.symbol == "SPY"
    assert analytics.has_trade_detail is True
    assert analytics.has_equity_curve is True
    assert analytics.trade_count == len(run_result.result.trades)


def test_analyze_experiment_degrades_gracefully_for_pre_sprint9_experiment(tmp_path):
    # An experiment logged the old way (log_experiment only, no
    # save_trades/save_equity_curve/save_spec ever called) must still
    # analyze -- with undefined metrics, never a crash or a fabricated
    # number (Sprint 9 spec, section 19).
    registry = _make_registry(tmp_path)
    legacy_id = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP", strategy_name="legacy"
    )

    analytics = AnalyticsService().analyze_experiment(registry, legacy_id)
    assert analytics is not None
    assert analytics.has_trade_detail is False
    assert analytics.has_equity_curve is False
    assert analytics.strategy_name is None  # no ExperimentSpec was ever saved
    assert_undefined(analytics.total_pnl, "no equity observations")
    assert_undefined(analytics.win_rate, "no closed trades")
    assert analytics.trade_count == 0


# ---------------------------------------------------------------------------
# compare_experiments -- warnings, never a composite "best" score
# ---------------------------------------------------------------------------


def test_compare_experiments_single_experiment_has_no_warnings(tmp_path):
    registry = _make_registry(tmp_path)
    run_result = _run_and_persist(registry)
    analytics = AnalyticsService().analyze_experiment(registry, run_result.experiment_id)

    result = compare_experiments([analytics])
    assert isinstance(result, ComparisonResult)
    assert result.rows == [analytics]
    assert result.warnings == []


def test_compare_experiments_identical_symbol_and_timeframe_has_no_warnings(tmp_path):
    registry = _make_registry(tmp_path)
    run_a = _run_and_persist(registry, symbol="SPY", strategy="ema_cross", params={"fast": 2, "slow": 4})
    run_b = _run_and_persist(registry, symbol="SPY", strategy="ema_cross", params={"fast": 3, "slow": 6})

    a = AnalyticsService().analyze_experiment(registry, run_a.experiment_id)
    b = AnalyticsService().analyze_experiment(registry, run_b.experiment_id)
    result = compare_experiments([a, b])
    assert result.warnings == []


def test_compare_experiments_flags_material_differences(tmp_path):
    registry = _make_registry(tmp_path)
    run_a = _run_and_persist(registry, symbol="SPY", strategy="ema_cross")
    run_b = _run_and_persist(registry, symbol="QQQ", strategy="rsi_mean_reversion", params={"period": 5})

    a = AnalyticsService().analyze_experiment(registry, run_a.experiment_id)
    b = AnalyticsService().analyze_experiment(registry, run_b.experiment_id)
    result = compare_experiments([a, b])

    flagged_fields = {w.field for w in result.warnings}
    assert "symbol" in flagged_fields
    assert "strategy_version" in flagged_fields
    for warning in result.warnings:
        assert isinstance(warning, ComparisonWarning)
        assert warning.values == ("SPY", "QQQ") or len(set(warning.values)) > 1


def test_compare_experiments_with_an_undefined_metric_in_one_experiment(tmp_path):
    # One real experiment plus one legacy (pre-Sprint-9) experiment with
    # no trades/equity at all -- comparison must not crash, and the
    # legacy row's undefined metrics must stay undefined, never
    # substituted with zero (Sprint 9 spec, section 14).
    registry = _make_registry(tmp_path)
    run_result = _run_and_persist(registry)
    legacy_id = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )

    a = AnalyticsService().analyze_experiment(registry, run_result.experiment_id)
    b = AnalyticsService().analyze_experiment(registry, legacy_id)
    result = compare_experiments([a, b])

    assert result.rows[1].total_pnl.status is MetricStatus.UNDEFINED
    assert result.rows[1].win_rate.value is None


def test_comparison_result_never_carries_a_composite_score():
    # Sprint 9 spec, section 15: no hidden "best strategy" ranking.
    # Structural check on the shape itself, not just behavior.
    field_names = {f for f in ComparisonResult.__dataclass_fields__}
    assert field_names == {"rows", "warnings"}
    analytics_fields = set(BacktestAnalytics.__dataclass_fields__)
    forbidden = {"score", "rank", "best", "composite"}
    assert not (analytics_fields & forbidden)
