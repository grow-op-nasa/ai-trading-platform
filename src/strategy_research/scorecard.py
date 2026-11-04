"""Strategy scorecards -- one typed, deterministic result per evaluated window (Sprint 23, ADR-0061).

Reuses ``src.analytics`` for every metric that already exists there (Sharpe, volatility, drawdown, exposure) so no
screen carries a second implementation. Anything undefined is an explicit ``Metric.undefined(reason)`` -- never a
silent zero. Interpretation notes are derived from the measured values only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.analytics import metrics as am
from src.analytics.models import Metric, MetricStatus
from src.backtesting.models import Trade

MIN_RELIABLE_TRADES = 30
VERY_SMALL_SAMPLE = 10
MIN_ANNUALISATION_BARS = 60
MIN_RATIO_BARS = 20
CONCENTRATION_SHARE = 0.5


def metric_to_dict(m: Metric) -> dict[str, Any]:
    return {"value": m.value, "status": m.status.value, "reason": m.reason}


def metric_from_dict(raw: dict[str, Any]) -> Metric:
    if raw.get("status") == MetricStatus.OK.value:
        return Metric.of(float(raw["value"]))
    return Metric.undefined(raw.get("reason") or "undefined")


@dataclass(frozen=True)
class Scorecard:
    """Metrics for one window (a segment of a run) of one strategy configuration on one instrument."""

    label: str
    first_bar: str
    last_bar: str
    bars: int
    closed_trades: int
    open_trades_at_end: int
    win_rate: Metric
    average_win: Metric
    average_loss: Metric
    payoff_ratio: Metric
    expectancy: Metric                  # net dollars per closed trade
    profit_factor: Metric
    gross_return: Metric
    net_return: Metric
    cost_drag: Metric                   # gross - net, in return points
    net_return_at_stress_costs: Metric
    total_costs: Metric                 # dollars (fees + slippage)
    annualised_return: Metric
    annualised_volatility: Metric
    sharpe: Metric
    sortino: Metric
    max_drawdown: Metric
    average_holding_bars: Metric
    average_holding_days: Metric
    turnover: Metric                    # annualised traded notional / average equity
    exposure_time: Metric
    benchmark_return: Metric
    excess_return: Metric               # net - FULL buy-and-hold (not like-for-like: the strategy is partly invested)
    # Sprint 23 closeout (research engine v3). Three comparators are kept strictly apart:
    #   full buy-and-hold  = benchmark_* (same instrument, window and starting capital, fully invested throughout);
    #   exposure-matched   = same bars the strategy was invested, whole-bar instrument returns, uninvested bars in cash at 0%
    #                        (a retrospective comparator, never a tradable strategy);
    #   active             = the strategy's own net figures above.
    benchmark_volatility: Metric
    benchmark_sharpe: Metric
    benchmark_max_drawdown: Metric
    exposure_matched_return: Metric
    excess_vs_exposure_matched: Metric  # net - exposure-matched
    capital_utilisation_mean: Metric    # mean(position market value / equity) over the window's bars
    capital_utilisation_max: Metric
    sample: str                         # "NONE" | "VERY_SMALL" | "SMALL" | "ADEQUATE"
    notes: tuple[str, ...] = field(default_factory=tuple)
    stress_multiple: float = 3.0

    _METRICS = ("win_rate", "average_win", "average_loss", "payoff_ratio", "expectancy", "profit_factor",
                "gross_return", "net_return", "cost_drag", "net_return_at_stress_costs", "total_costs",
                "annualised_return", "annualised_volatility", "sharpe", "sortino", "max_drawdown",
                "average_holding_bars", "average_holding_days", "turnover", "exposure_time",
                "benchmark_return", "excess_return", "benchmark_volatility", "benchmark_sharpe",
                "benchmark_max_drawdown", "exposure_matched_return", "excess_vs_exposure_matched",
                "capital_utilisation_mean", "capital_utilisation_max")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {k: getattr(self, k) for k in ("label", "first_bar", "last_bar", "bars", "closed_trades",
                                                              "open_trades_at_end", "sample", "stress_multiple")}
        out["notes"] = list(self.notes)
        for k in self._METRICS:
            out[k] = metric_to_dict(getattr(self, k))
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Scorecard":
        kwargs: dict[str, Any] = {k: raw[k] for k in ("label", "first_bar", "last_bar", "bars", "closed_trades",
                                                       "open_trades_at_end", "sample")}
        kwargs["stress_multiple"] = raw.get("stress_multiple", 3.0)
        kwargs["notes"] = tuple(raw.get("notes", ()))
        for k in cls._METRICS:
            # Records written before the closeout carry no benchmark-risk / exposure-matched fields.
            kwargs[k] = metric_from_dict(raw[k]) if k in raw else Metric.undefined("not recorded by the engine version that made this run")
        return cls(**kwargs)


def sample_label(closed_trades: int) -> str:
    if closed_trades == 0:
        return "NONE"
    if closed_trades < VERY_SMALL_SAMPLE:
        return "VERY_SMALL"
    if closed_trades < MIN_RELIABLE_TRADES:
        return "SMALL"
    return "ADEQUATE"


def _net(t: Trade) -> float | None:
    return t.net_pnl


def _ratio(num: float, den: float, reason: str) -> Metric:
    return Metric.undefined(reason) if den == 0 else Metric.of(num / den)


def _segment_return(series: pd.Series, i0: int, i1: int) -> Metric:
    """Return across bars [i0, i1): from the bar before the segment (or the first bar) to its last bar."""
    if series is None or series.empty or i1 - i0 < 1:
        return Metric.undefined("no observations in the window")
    base = float(series.iloc[max(i0 - 1, 0)])
    if base <= 0:
        return Metric.undefined("non-positive starting value")
    return Metric.of(float(series.iloc[i1 - 1]) / base - 1.0)


def _sortino(returns: pd.Series, periods_per_year: int) -> Metric:
    if len(returns) < MIN_RATIO_BARS:
        return Metric.undefined(f"fewer than {MIN_RATIO_BARS} period returns")
    downside = np.minimum(returns.to_numpy(), 0.0)
    dd = float(np.sqrt(np.mean(downside ** 2)))
    if dd == 0:
        return Metric.undefined("no negative returns -- downside deviation is zero")
    return Metric.of(float(returns.mean() / dd * np.sqrt(periods_per_year)))


def invested_bars(index: pd.DatetimeIndex, trades: list[Trade]) -> tuple[np.ndarray, np.ndarray]:
    """Per-bar invested flag and invested notional-per-share-held map.

    Convention (documented, ADR-0061 amendment): a bar ``b`` counts as invested when ``entry_time < b <= exit_time``
    (signal timestamps). Under next-bar-open fills the position is genuinely held from the open of ``entry+1`` to the
    open of ``exit+1``, so this attributes whole bars close-to-close: it slightly mis-states the fill bar (open->close
    rather than close->close) and the exit gap. It is a comparator convention only, never used for strategy P&L.
    Returns ``(flags, quantity)`` where ``quantity[b]`` is the total signed shares held across trades on bar ``b``.
    """
    flags = np.zeros(len(index), dtype=bool)
    qty = np.zeros(len(index), dtype=float)
    for t in trades:
        mask = (index > t.entry_time) & (index <= t.exit_time)
        flags |= mask
        qty[mask] += float(t.quantity or 0) * (1 if t.direction >= 0 else -1)
    return flags, qty


def _exposure_matched(close: pd.Series, flags: np.ndarray, i0: int, i1: int) -> Metric:
    if i1 - i0 < 1 or close is None or close.empty:
        return Metric.undefined("no observations in the window")
    rets = close.pct_change().to_numpy()
    growth = 1.0
    for b in range(i0, i1):
        if flags[b] and not np.isnan(rets[b]):
            growth *= 1.0 + rets[b]
    return Metric.of(growth - 1.0)


def _utilisation(close: pd.Series, qty: np.ndarray, net_equity: pd.Series, i0: int, i1: int) -> tuple[Metric, Metric]:
    vals = []
    for b in range(i0, i1):
        eq = float(net_equity.iloc[b])
        if eq > 0:
            vals.append(abs(qty[b]) * float(close.iloc[b]) / eq)
    if not vals:
        return Metric.undefined("no observations in the window"), Metric.undefined("no observations in the window")
    return Metric.of(float(np.mean(vals))), Metric.of(float(np.max(vals)))


def build_scorecard(
    *,
    label: str,
    i0: int,
    i1: int,
    index: pd.DatetimeIndex,
    net_trades: list[Trade],
    net_equity: pd.Series,
    gross_equity: pd.Series,
    stress_equity: pd.Series,
    close: pd.Series,
    initial_cash: float,
    stress_multiple: float = 3.0,
) -> Scorecard:
    """Scorecard for bars ``[i0, i1)`` of a continuous run.

    A trade belongs to the window in which it *exits* (a still-open position at the end of the data belongs to the
    last window and is counted as open, not as a closed trade). Window returns chain exactly: each starts from the
    value at the bar before it, so consecutive windows compound to the whole-run return.
    """
    t0, t1 = index[i0], index[i1 - 1]
    in_window = [t for t in net_trades if t0 <= t.exit_time <= t1]
    open_trades = [t for t in in_window if t.exit_signal_id is None]
    closed = [t for t in in_window if t.exit_signal_id is not None]
    n = len(closed)
    pnls = [p for p in (_net(t) for t in closed) if p is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    base_i = max(i0 - 1, 0)
    eq = net_equity.iloc[base_i:i1]
    seg_returns = eq.pct_change().dropna()
    ppy = 252
    if len(index) > 1:
        from src.backtesting.metrics import infer_periods_per_year
        ppy = infer_periods_per_year(index) or 252

    net_ret = _segment_return(net_equity, i0, i1)
    gross_ret = _segment_return(gross_equity, i0, i1)
    stress_ret = _segment_return(stress_equity, i0, i1)
    bench = _segment_return(close, i0, i1)

    if n == 0:
        win_rate = Metric.undefined("no closed trades")
        avg_win = avg_loss = payoff = expectancy = pf = Metric.undefined("no closed trades")
    else:
        win_rate = Metric.of(len(wins) / n)
        avg_win = Metric.of(float(np.mean(wins))) if wins else Metric.undefined("no winning trades")
        avg_loss = Metric.of(float(np.mean(losses))) if losses else Metric.undefined("no losing trades")
        payoff = (Metric.of(float(np.mean(wins)) / abs(float(np.mean(losses)))) if wins and losses
                  else Metric.undefined("needs at least one winning and one losing trade"))
        expectancy = Metric.of(float(np.mean(pnls))) if pnls else Metric.undefined("no trade P&L recorded")
        if losses:
            pf = Metric.of(sum(wins) / abs(sum(losses)))
        else:
            pf = Metric.undefined("no losing trades -- profit factor is not defined")

    if n:
        bars_held = [len(index[(index >= t.entry_time) & (index <= t.exit_time)]) - 1 for t in closed]
        days_held = [(t.exit_time - t.entry_time).total_seconds() / 86400.0 for t in closed]
        hold_bars = Metric.of(float(np.mean(bars_held)))
        hold_days = Metric.of(float(np.mean(days_held)))
    else:
        hold_bars = hold_days = Metric.undefined("no closed trades")

    costs = sum((t.total_fees + (t.slippage_cost or 0.0)) for t in in_window)
    traded = sum(float(t.quantity or 0) * ((t.entry_fill_price or t.entry_price) + (t.exit_fill_price or t.exit_price))
                 for t in in_window)
    avg_equity = float(eq.mean()) if len(eq) else float("nan")
    years = max((t1 - index[base_i]).total_seconds() / (365.25 * 86400.0), 0.0)
    if years > 0 and avg_equity > 0:
        turnover = Metric.of(traded / avg_equity / years)
    else:
        turnover = Metric.undefined("window has no elapsed time")

    bars = i1 - i0
    if bars >= MIN_ANNUALISATION_BARS and net_ret.is_defined and years > 0 and (1 + net_ret.value) > 0:
        ann = Metric.of((1 + net_ret.value) ** (1 / years) - 1)
    else:
        ann = Metric.undefined(f"window shorter than {MIN_ANNUALISATION_BARS} bars -- not annualised")
    if len(seg_returns) >= MIN_RATIO_BARS:
        vol = am.volatility(eq, ppy).metric
        sharpe = am.sharpe_ratio(eq, ppy).metric
    else:
        vol = sharpe = Metric.undefined(f"fewer than {MIN_RATIO_BARS} period returns")
    exposure = am.exposure_time(in_window, eq) if len(eq) >= 2 else Metric.undefined("window too short")

    excess = (Metric.of(net_ret.value - bench.value) if net_ret.is_defined and bench.is_defined
              else Metric.undefined("needs strategy and benchmark returns"))
    drag = (Metric.of(gross_ret.value - net_ret.value) if gross_ret.is_defined and net_ret.is_defined
            else Metric.undefined("needs gross and net returns"))

    bench_series = close.iloc[base_i:i1]
    if len(bench_series.pct_change().dropna()) >= MIN_RATIO_BARS:
        b_vol = am.volatility(bench_series, ppy).metric
        b_sharpe = am.sharpe_ratio(bench_series, ppy).metric
    else:
        b_vol = b_sharpe = Metric.undefined(f"fewer than {MIN_RATIO_BARS} period returns")
    b_dd = am.max_drawdown(bench_series) if len(bench_series) >= 2 else Metric.undefined("window too short")
    flags, qty = invested_bars(index, [t for t in net_trades if t.exit_time >= index[base_i]])
    matched = _exposure_matched(close, flags, i0, i1)
    excess_matched = (Metric.of(net_ret.value - matched.value) if net_ret.is_defined and matched.is_defined
                      else Metric.undefined("needs strategy and exposure-matched returns"))
    util_mean, util_max = _utilisation(close, qty, net_equity, i0, i1)

    card = Scorecard(
        label=label, first_bar=t0.isoformat(), last_bar=t1.isoformat(), bars=bars, closed_trades=n,
        open_trades_at_end=len(open_trades), win_rate=win_rate, average_win=avg_win, average_loss=avg_loss,
        payoff_ratio=payoff, expectancy=expectancy, profit_factor=pf, gross_return=gross_ret, net_return=net_ret,
        cost_drag=drag, net_return_at_stress_costs=stress_ret, total_costs=Metric.of(float(costs)),
        annualised_return=ann, annualised_volatility=vol, sharpe=sharpe,
        sortino=_sortino(seg_returns, ppy), max_drawdown=am.max_drawdown(eq), average_holding_bars=hold_bars,
        average_holding_days=hold_days, turnover=turnover, exposure_time=exposure, benchmark_return=bench,
        excess_return=excess, benchmark_volatility=b_vol, benchmark_sharpe=b_sharpe, benchmark_max_drawdown=b_dd,
        exposure_matched_return=matched, excess_vs_exposure_matched=excess_matched,
        capital_utilisation_mean=util_mean, capital_utilisation_max=util_max,
        sample=sample_label(n), stress_multiple=stress_multiple,
    )
    return _with_notes(card, pnls)


def _with_notes(card: Scorecard, pnls: list[float]) -> Scorecard:
    notes: list[str] = []
    if card.closed_trades == 0:
        notes.append("No closed trades in this window: there is nothing to evaluate. A quiet strategy is not a failed one.")
    elif card.sample in ("VERY_SMALL", "SMALL"):
        notes.append(f"Only {card.closed_trades} closed trades (reliable conclusions need roughly {MIN_RELIABLE_TRADES}+): "
                     "treat every figure here as anecdotal.")
    if card.win_rate.is_defined and card.expectancy.is_defined and card.win_rate.value >= 0.5 and card.expectancy.value <= 0:
        notes.append(f"Win rate is {card.win_rate.value:.0%} but expectancy is not positive: the losses are larger than the wins.")
    if card.gross_return.is_defined and card.net_return.is_defined and card.gross_return.value > 0 >= card.net_return.value:
        notes.append("Profitable before simulated costs, not after: no edge once frictions are included.")
    if (card.net_return.is_defined and card.net_return_at_stress_costs.is_defined and card.net_return.value > 0
            >= card.net_return_at_stress_costs.value):
        notes.append(f"The positive net return does not survive {card.stress_multiple:g}x the assumed costs: cost-fragile.")
    gross_profit = sum(p for p in pnls if p > 0)
    if len(pnls) >= 3 and gross_profit > 0 and max(pnls) / gross_profit > CONCENTRATION_SHARE:
        notes.append(f"{max(pnls) / gross_profit:.0%} of gross profit comes from a single trade: results depend on one outlier.")
    if card.excess_return.is_defined and card.excess_return.value < 0 and card.benchmark_return.is_defined:
        notes.append("Underperformed buy-and-hold of the same instrument over the same window (the strategy is only "
                     "invested part of the time, so this is not by itself a rejection).")
    if (card.excess_vs_exposure_matched.is_defined and card.excess_vs_exposure_matched.value < 0
            and card.exposure_matched_return.is_defined):
        notes.append("Also trailed the exposure-matched comparator (the same instrument held only on the bars the strategy "
                     "was invested): the entry/exit timing itself did not add value in this window.")
    return Scorecard(**{**{k: getattr(card, k) for k in card.__dataclass_fields__}, "notes": tuple(notes)})


def yearly_returns(index: pd.DatetimeIndex, net_equity: pd.Series, close: pd.Series, i0: int, i1: int) -> list[dict[str, Any]]:
    """Net and buy-and-hold return per calendar year within bars ``[i0, i1)`` (years are chained from the previous bar)."""
    out: list[dict[str, Any]] = []
    years = pd.Series(index[i0:i1].year, index=range(i0, i1))
    for year, pos in years.groupby(years):
        a, b = int(pos.index.min()), int(pos.index.max()) + 1
        net = _segment_return(net_equity, a, b)
        bench = _segment_return(close, a, b)
        out.append({"year": int(year), "bars": b - a, "net_return": metric_to_dict(net), "benchmark_return": metric_to_dict(bench)})
    return out
