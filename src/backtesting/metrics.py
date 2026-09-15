"""Backtest metrics: turn a trade list + equity curve into numbers.

Each function is pure and independently testable -- `calculate_metrics`
is the one the Backtester actually calls; the rest are its building
blocks, exposed separately so they can be tested (and reused, e.g. by
the Experiment Registry or a future analytics module) on their own.
"""

from __future__ import annotations

import pandas as pd

from src.backtesting.models import Trade

# The historical default before ADR-0038: correct for genuine daily
# (one-bar-per-trading-day) data, and still the fallback when a return
# series is too short to infer a bar frequency from at all. Never
# applied unconditionally to every timeframe anymore -- see
# `infer_periods_per_year()`.
DEFAULT_PERIODS_PER_YEAR = 252

_TRADING_DAYS_PER_YEAR = 252


def infer_periods_per_year(index: pd.DatetimeIndex) -> int | None:
    """Estimate how many bars a year of `index`'s own spacing implies.

    `DECISIONS.md`, ADR-0038: annualizing every backtest's Sharpe ratio
    with a flat 252 (trading days/year) silently assumed one candle
    equals one trading day -- correct for daily bars, wrong by orders
    of magnitude for intraday ones (a 1-minute return annualized as if
    it were a full trading day's return wildly overstates Sharpe).
    Estimates bars-per-day from the *median* gap between consecutive
    timestamps (robust to the occasional weekend/holiday gap in daily
    data), then scales by `_TRADING_DAYS_PER_YEAR` -- for daily bars
    (median gap = 1 day) this reduces to exactly the historical 252
    default; for intraday bars it scales up accordingly.

    Deliberately calendar-time-based, not exchange-session-aware: it
    does not know NYSE hours, holidays, or that crypto trades 24/7, so
    intraday estimates are an order-of-magnitude-correct approximation,
    not a precise one. Modeling actual trading-session length is real
    future work (`ROADMAP.md`), not required to stop annualization from
    being silently wrong for non-daily bars.

    Returns:
        `None` if `index` has fewer than 2 timestamps, or their median
        gap is non-positive (degenerate input) -- callers should fall
        back to `DEFAULT_PERIODS_PER_YEAR` in that case, the same way
        `calculate_metrics`/`sharpe_ratio` do.
    """
    if len(index) < 2:
        return None
    median_gap = pd.Series(index).diff().median()
    if pd.isna(median_gap) or median_gap <= pd.Timedelta(0):
        return None
    bars_per_day = pd.Timedelta(days=1) / median_gap
    periods_per_year = round(bars_per_day * _TRADING_DAYS_PER_YEAR)
    return max(periods_per_year, 1)


def calculate_metrics(
    trades: list[Trade],
    equity_curve: pd.Series,
    initial_cash: float,
    periods_per_year: int | None = None,
) -> dict:
    """Compute the standard metrics set for a completed backtest.

    Args:
        periods_per_year: annualization factor for `sharpe`. Defaults to
            `None`, which infers it from `equity_curve`'s own timestamp
            spacing (`infer_periods_per_year`) -- correct for whatever
            timeframe the backtest actually ran at, daily or intraday,
            rather than silently assuming daily bars (`DECISIONS.md`,
            ADR-0038). Pass an explicit value to override.

    Returns a dict with keys: `total_trades`, `win_rate`, `sharpe`,
    `total_return_pct`, `max_drawdown_pct`. `win_rate` and `sharpe` are
    `None` when they can't be meaningfully computed (no trades, or a
    return series too short/constant to have a defined Sharpe ratio) --
    `None` rather than 0, since 0 would falsely imply "computed and
    equal to zero."
    """
    total_return_pct = (
        equity_curve.iloc[-1] / initial_cash - 1 if len(equity_curve) else 0.0
    )
    max_drawdown_pct = max_drawdown(equity_curve)

    if not trades:
        return {
            "total_trades": 0,
            "win_rate": None,
            "sharpe": None,
            "total_return_pct": total_return_pct,
            "max_drawdown_pct": max_drawdown_pct,
        }

    returns = [t.return_pct for t in trades]
    win_rate = sum(1 for r in returns if r > 0) / len(returns)

    period_returns = equity_curve.pct_change().dropna()
    sharpe = sharpe_ratio(period_returns, periods_per_year=periods_per_year)

    return {
        "total_trades": len(trades),
        "win_rate": win_rate,
        "sharpe": sharpe,
        "total_return_pct": total_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
    }


def sharpe_ratio(
    period_returns: pd.Series, periods_per_year: int | None = None
) -> float | None:
    """Annualized Sharpe ratio (assumes zero risk-free rate).

    Args:
        periods_per_year: defaults to `None`, which infers the
            annualization factor from `period_returns`'s own
            `DatetimeIndex` (`infer_periods_per_year`), falling back to
            `DEFAULT_PERIODS_PER_YEAR` (252) when that isn't possible
            (e.g. too few observations, or a non-`DatetimeIndex`).
            `DECISIONS.md`, ADR-0038: a flat, unconditional 252 here
            silently assumed daily bars regardless of what timeframe
            actually produced `period_returns`.

    Returns None if there are fewer than 2 return observations or the
    return series has zero variance (undefined Sharpe), rather than
    raising or returning a misleading 0.0/inf.
    """
    if len(period_returns) < 2:
        return None
    std = period_returns.std(ddof=0)
    if std == 0:
        return None
    if periods_per_year is None:
        index = period_returns.index
        inferred = (
            infer_periods_per_year(index)
            if isinstance(index, pd.DatetimeIndex)
            else None
        )
        periods_per_year = inferred if inferred is not None else DEFAULT_PERIODS_PER_YEAR
    return float((period_returns.mean() / std) * (periods_per_year ** 0.5))


def max_drawdown(equity_curve: pd.Series) -> float:
    """Largest peak-to-trough decline in the equity curve, as a negative fraction.

    e.g. -0.15 means a 15% drawdown from the running peak at its worst
    point. Returns 0.0 for an empty equity curve.
    """
    if equity_curve.empty:
        return 0.0
    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    return float(drawdown.min())
