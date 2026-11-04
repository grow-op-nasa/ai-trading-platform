"""Strategy scorecards (src/strategy_research/scorecard.py, Sprint 23, ADR-0061).

Trades and equity curves are fabricated so every expected number can be checked by hand.
"""

from __future__ import annotations

import math
from uuid import uuid4

import pandas as pd
import pytest

from src.analytics.models import MetricStatus
from src.backtesting.models import Trade
from src.strategy_research.scorecard import (
    MIN_RELIABLE_TRADES,
    Scorecard,
    _sortino,
    build_scorecard,
    sample_label,
    yearly_returns,
)

INDEX = pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC")
CLOSE = pd.Series([10, 10, 11, 11, 11, 10.5, 10.5, 10.5, 10, 10], index=INDEX, dtype=float)
NET = pd.Series([100000, 100000, 100098, 100098, 100098, 100044, 100044, 100044, 100044, 100044], index=INDEX, dtype=float)
GROSS = pd.Series([100000, 100000, 100100, 100100, 100100, 100050, 100050, 100050, 100050, 100050], index=INDEX, dtype=float)
STRESS = pd.Series([100000, 100000, 100090, 100090, 100090, 100030, 100030, 100030, 100030, 100030], index=INDEX, dtype=float)


def trade(i_in, i_out, qty, entry, exit_, *, e_fill=None, x_fill=None, fee=0.0, closed=True, index=INDEX) -> Trade:
    return Trade(entry_time=index[i_in], exit_time=index[i_out], direction=1, entry_price=entry, exit_price=exit_,
                 entry_signal_id=uuid4(), exit_signal_id=uuid4() if closed else None, quantity=qty,
                 entry_fill_price=e_fill, exit_fill_price=x_fill, entry_fee=fee, exit_fee=fee)


# T1: gross +100, no slippage, fees 1+1  -> net +98
T1 = trade(1, 2, 100, 10.0, 11.0, e_fill=10.0, x_fill=11.0, fee=1.0)
# T2: gross -50; fills 11.01 / 10.49 -> fill P&L -52 -> slippage cost 2; fees 1+1 -> net -54
T2 = trade(4, 5, 100, 11.0, 10.5, e_fill=11.01, x_fill=10.49, fee=1.0)
# T3: still open when the data ends (no exit signal), qty 10, flat price
T3 = trade(8, 9, 10, 10.0, 10.0, closed=False)


def card(trades, i0=0, i1=10, net=NET, gross=GROSS, stress=STRESS, close=CLOSE, index=INDEX, label="t") -> Scorecard:
    return build_scorecard(label=label, i0=i0, i1=i1, index=index, net_trades=trades, net_equity=net, gross_equity=gross,
                           stress_equity=stress, close=close, initial_cash=100000.0)


def val(m):
    assert m.status is MetricStatus.OK, m.reason
    return m.value


def test_trade_economics_are_what_the_test_assumes():
    assert (T1.gross_pnl, T1.slippage_cost, T1.net_pnl) == (100.0, 0.0, 98.0)
    assert T2.gross_pnl == -50.0 and T2.net_pnl == pytest.approx(-54.0) and T2.slippage_cost == pytest.approx(2.0)


def test_trade_statistics_match_hand_calculation():
    c = card([T1, T2, T3])
    assert c.closed_trades == 2 and c.open_trades_at_end == 1
    assert val(c.win_rate) == 0.5
    assert val(c.average_win) == 98.0
    assert val(c.average_loss) == pytest.approx(-54.0)
    assert val(c.payoff_ratio) == pytest.approx(98.0 / 54.0)
    assert val(c.expectancy) == pytest.approx((98.0 - 54.0) / 2)            # 22
    assert val(c.profit_factor) == pytest.approx(98.0 / 54.0)
    assert val(c.total_costs) == pytest.approx(1 + 1 + 1 + 1 + 2)            # four 1-dollar fees + 2 slippage on T2
    assert c.sample == "VERY_SMALL"


def test_gross_net_cost_drag_and_stress_returns():
    c = card([T1, T2, T3])
    assert val(c.net_return) == pytest.approx(100044 / 100000 - 1)
    assert val(c.gross_return) == pytest.approx(100050 / 100000 - 1)
    assert val(c.cost_drag) == pytest.approx(100050 / 100000 - 100044 / 100000)
    assert val(c.net_return_at_stress_costs) == pytest.approx(100030 / 100000 - 1)


def test_benchmark_is_the_same_window_buy_and_hold():
    c = card([T1, T2, T3])
    assert val(c.benchmark_return) == pytest.approx(10 / 10 - 1)             # first close 10, last close 10
    assert val(c.excess_return) == pytest.approx(100044 / 100000 - 1 - 0.0)
    w = card([T1], i0=5, i1=10)
    assert val(w.benchmark_return) == pytest.approx(10 / 11 - 1)             # from the bar before the window (11) to its last (10)


def test_exposure_matched_comparator_is_built_from_the_invested_bars_only():
    c = card([T1, T2, T3])
    # Invested bars (entry < bar <= exit): 2 (T1), 5 (T2), 9 (T3). Close-to-close returns: +10%, 10.5/11-1, 0%; cash otherwise.
    assert val(c.exposure_matched_return) == pytest.approx(1.1 * (10.5 / 11) - 1)           # = 5.00%
    assert val(c.exposure_matched_return) == pytest.approx(0.05)
    assert val(c.excess_vs_exposure_matched) == pytest.approx(100044 / 100000 - 1 - 0.05)
    # The full buy-and-hold comparator is a different number and stays separate.
    assert val(c.benchmark_return) == pytest.approx(0.0)


def test_capital_utilisation_is_position_value_over_equity():
    c = card([T1, T2, T3])
    expected = [100 * 11 / 100098, 100 * 10.5 / 100044, 10 * 10 / 100044]
    assert val(c.capital_utilisation_max) == pytest.approx(max(expected))
    assert val(c.capital_utilisation_mean) == pytest.approx(sum(expected) / 10)


def test_benchmark_drawdown_and_undefined_benchmark_ratios():
    c = card([T1, T2, T3])
    assert abs(val(c.benchmark_max_drawdown)) == pytest.approx(1 / 11)                       # peak 11 -> trough 10
    assert c.benchmark_sharpe.status is not MetricStatus.OK and "fewer than" in c.benchmark_sharpe.reason
    assert c.benchmark_volatility.status is not MetricStatus.OK


def test_a_no_trade_window_has_zero_exposure_matched_return_and_zero_utilisation():
    c = card([])
    assert val(c.exposure_matched_return) == 0.0 and val(c.capital_utilisation_max) == 0.0


def test_old_records_without_the_new_fields_still_load_with_explicit_undefined():
    raw = card([T1, T2, T3]).to_dict()
    for k in ("benchmark_volatility", "benchmark_sharpe", "benchmark_max_drawdown", "exposure_matched_return",
              "excess_vs_exposure_matched", "capital_utilisation_mean", "capital_utilisation_max"):
        raw.pop(k)
    old = Scorecard.from_dict(raw)
    assert old.exposure_matched_return.status is not MetricStatus.OK and "not recorded" in old.exposure_matched_return.reason


def test_window_returns_chain_exactly_to_the_whole_run_return():
    a, b = card([T1, T2, T3], 0, 5), card([T1, T2, T3], 5, 10)
    assert (1 + val(a.net_return)) * (1 + val(b.net_return)) - 1 == pytest.approx(val(card([T1, T2, T3]).net_return), abs=1e-12)
    assert (1 + val(a.benchmark_return)) * (1 + val(b.benchmark_return)) - 1 == pytest.approx(0.0, abs=1e-12)


def test_a_trade_belongs_to_the_window_in_which_it_exits_and_open_trades_to_the_last():
    first, second = card([T1, T2, T3], 0, 5), card([T1, T2, T3], 5, 10)
    assert (first.closed_trades, first.open_trades_at_end) == (1, 0)
    assert (second.closed_trades, second.open_trades_at_end) == (1, 1)
    assert val(first.win_rate) == 1.0 and val(second.win_rate) == 0.0


def test_undefined_metrics_are_explicit_never_zero():
    first = card([T1, T2, T3], 0, 5)
    for m, text in ((first.average_loss, "no losing"), (first.profit_factor, "no losing"), (first.payoff_ratio, "at least one")):
        assert m.status is MetricStatus.UNDEFINED and m.value is None and text in m.reason
    second = card([T1, T2, T3], 5, 10)
    assert second.average_win.status is MetricStatus.UNDEFINED and "no winning" in second.average_win.reason
    assert val(second.profit_factor) == 0.0                                  # losses and no gross profit: a real zero


def test_zero_trades_window():
    flat = pd.Series(100000.0, index=INDEX)
    c = card([], net=flat, gross=flat, stress=flat)
    assert c.closed_trades == 0 and c.sample == "NONE"
    for m in (c.win_rate, c.average_win, c.average_loss, c.expectancy, c.profit_factor, c.payoff_ratio):
        assert m.status is MetricStatus.UNDEFINED and "no closed trades" in m.reason
    assert val(c.total_costs) == 0.0 and val(c.exposure_time) == 0.0 and val(c.net_return) == 0.0
    assert c.sharpe.status is MetricStatus.UNDEFINED                          # too few observations
    assert any("nothing to evaluate" in n for n in c.notes)


def test_short_window_is_not_annualised_and_long_window_is():
    assert card([T1, T2, T3]).annualised_return.status is MetricStatus.UNDEFINED
    n = 100
    idx = pd.date_range("2023-01-02", periods=n, freq="D", tz="UTC")
    eq = pd.Series([100000 * 1.001 ** k for k in range(n)], index=idx)
    close = pd.Series(range(100, 100 + n), index=idx, dtype=float)
    c = build_scorecard(label="x", i0=0, i1=n, index=idx, net_trades=[], net_equity=eq, gross_equity=eq, stress_equity=eq,
                        close=close, initial_cash=100000.0)
    years = 99 / 365.25
    assert val(c.annualised_return) == pytest.approx((1.001 ** 99) ** (1 / years) - 1)
    flat = pd.Series(100000.0, index=idx)
    f = build_scorecard(label="x", i0=0, i1=n, index=idx, net_trades=[], net_equity=flat, gross_equity=flat, stress_equity=flat,
                        close=close, initial_cash=100000.0)
    assert f.sharpe.status is MetricStatus.UNDEFINED and "zero variance" in f.sharpe.reason
    assert f.sortino.status is MetricStatus.UNDEFINED and "no negative" in f.sortino.reason


def test_max_drawdown_is_peak_to_trough():
    idx = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    eq = pd.Series([100.0, 110.0, 99.0, 105.0], index=idx)
    c = build_scorecard(label="x", i0=0, i1=4, index=idx, net_trades=[], net_equity=eq, gross_equity=eq, stress_equity=eq,
                        close=eq, initial_cash=100.0)
    assert val(c.max_drawdown) == pytest.approx(99.0 / 110.0 - 1)


def test_sortino_matches_hand_calculation_and_edge_cases():
    r = pd.Series([0.01, -0.005] * 10)
    expected = 0.0025 / math.sqrt(0.5 * 0.005 ** 2) * math.sqrt(252)          # mean / downside deviation, annualised
    assert val(_sortino(r, 252)) == pytest.approx(expected, rel=1e-12)
    assert _sortino(pd.Series([0.01] * 25), 252).status is MetricStatus.UNDEFINED
    assert _sortino(pd.Series([0.01, -0.01]), 252).status is MetricStatus.UNDEFINED          # too few observations


def test_average_holding_period_and_turnover():
    c = card([T1, T2, T3])
    assert val(c.average_holding_bars) == 1.0 and val(c.average_holding_days) == 1.0        # both closed trades held one bar
    traded = 100 * (10 + 11) + 100 * (11.01 + 10.49) + 10 * (10 + 10)                       # 2100 + 2150 + 200
    avg_equity = float(NET.mean())
    assert val(c.turnover) == pytest.approx(traded / avg_equity / (9 / 365.25))


def test_exposure_time_counts_only_time_in_trades():
    c = card([T1, T2])
    assert val(c.exposure_time) == pytest.approx((1 + 1) / 9)                                # two one-day trades over nine days


@pytest.mark.parametrize("n,label", [(0, "NONE"), (1, "VERY_SMALL"), (9, "VERY_SMALL"), (10, "SMALL"), (29, "SMALL"),
                                     (MIN_RELIABLE_TRADES, "ADEQUATE"), (200, "ADEQUATE")])
def test_sample_size_labels(n, label):
    assert sample_label(n) == label


def test_note_high_win_rate_with_negative_expectancy():
    wins = [trade(1, 2, 100, 10.0, 10.1, e_fill=10.0, x_fill=10.1), trade(3, 4, 100, 10.0, 10.1, e_fill=10.0, x_fill=10.1)]
    loss = trade(5, 6, 100, 10.0, 8.0, e_fill=10.0, x_fill=8.0)
    c = card(wins + [loss])
    assert val(c.win_rate) == pytest.approx(2 / 3) and val(c.expectancy) < 0
    assert any("losses are larger than the wins" in n for n in c.notes)


def test_note_profitable_before_costs_only():
    gross = pd.Series([100000, 100000, 100100, 100100, 100100, 100100, 100100, 100100, 100100, 100100], index=INDEX, dtype=float)
    net = pd.Series([100000, 100000, 99990, 99990, 99990, 99990, 99990, 99990, 99990, 99990], index=INDEX, dtype=float)
    c = card([T1], net=net, gross=gross)
    assert val(c.gross_return) > 0 > val(c.net_return)
    assert any("Profitable before simulated costs" in n for n in c.notes)


def test_note_cost_fragile_when_profit_does_not_survive_stress_costs():
    stress = pd.Series([100000] * 10, index=INDEX, dtype=float) - 5
    stress.iloc[0] = 100000.0
    c = card([T1, T2], stress=stress)
    assert val(c.net_return) > 0 >= val(c.net_return_at_stress_costs)
    assert any("does not survive 3x" in n for n in c.notes)


def test_note_single_trade_concentration_and_small_sample():
    big = trade(1, 2, 100, 10.0, 20.0, e_fill=10.0, x_fill=20.0)
    small = [trade(3, 4, 100, 10.0, 10.1, e_fill=10.0, x_fill=10.1), trade(5, 6, 100, 10.0, 10.1, e_fill=10.0, x_fill=10.1)]
    c = card([big] + small)
    assert any("single trade" in n for n in c.notes) and any("anecdotal" in n for n in c.notes)


def test_scorecard_round_trips_through_its_stored_form_exactly():
    c = card([T1, T2, T3])
    assert Scorecard.from_dict(c.to_dict()).to_dict() == c.to_dict()
    undefined = card([])
    again = Scorecard.from_dict(undefined.to_dict())
    assert again.win_rate.status is MetricStatus.UNDEFINED and again.win_rate.reason == undefined.win_rate.reason


def test_yearly_returns_chain_across_calendar_years():
    idx = pd.to_datetime(["2023-12-28", "2023-12-29", "2024-01-02", "2024-01-03"], utc=True)
    eq = pd.Series([100.0, 110.0, 99.0, 108.9], index=idx)
    close = pd.Series([10.0, 10.0, 11.0, 11.0], index=idx)
    rows = yearly_returns(idx, eq, close, 0, 4)
    assert [r["year"] for r in rows] == [2023, 2024]
    r23, r24 = rows[0]["net_return"]["value"], rows[1]["net_return"]["value"]
    assert r23 == pytest.approx(110 / 100 - 1)                      # base is the first bar
    assert r24 == pytest.approx(108.9 / 110 - 1)                    # chained from the previous bar (110)
    assert rows[1]["benchmark_return"]["value"] == pytest.approx(11 / 10 - 1)
