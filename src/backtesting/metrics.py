"""Backtest metrics: turn a trade list + equity curve into numbers.

Each function is pure and independently testable -- `calculate_metrics`
is the one the Backtester actually calls; the rest are its building
blocks, exposed separately so they can be tested (and reused, e.g. by
the Experiment Registry or a future analytics module) on their own.
"""

from __future__ import annotations

import pandas as pd

from src.backtesting.models import Trade


def calculate_metrics(
    trades: list[Trade],
    equity_curve: pd.Series,
    initial_cash: float,
    periods_per_year: int = 252,
) -> dict:
    """Compute the standard metrics set for a completed backtest.

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


def sharpe_ratio(period_returns: pd.Series, periods_per_year: int = 252) -> float | None:
    """Annualized Sharpe ratio (assumes zero risk-free rate).

    Returns None if there are fewer than 2 return observations or the
    return series has zero variance (undefined Sharpe), rather than
    raising or returning a misleading 0.0/inf.
    """
    if len(period_returns) < 2:
        return None
    std = period_returns.std(ddof=0)
    if std == 0:
        return None
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
