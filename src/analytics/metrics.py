"""Deterministic metric calculations -- src/analytics/metrics.py.

Each function is pure: given the same `trades`/`equity_curve` input, it
always returns the same `Metric` -- no random numbers, no wall-clock
reads, no network calls, no LLM involvement anywhere in this module
(Sprint 9 spec, section 7). Every function represents "can't be
computed" as an explicit `Metric.undefined(reason)`, never a silent
`0`, `inf`, or `nan` (section 9).

Reuses `src.backtesting.metrics.infer_periods_per_year` for Sharpe/
volatility annualization rather than reinventing it -- that function
already implements the timeframe-aware annualization Sprint 9 section 8
asks for (`DECISIONS.md`, ADR-0038), and `src.analytics` depending on
`src.backtesting` for this pure helper is the sanctioned direction
(analytics sits downstream of backtesting, never the reverse).

Per-trade metrics (profit factor, expectancy, average/largest winner
and loser) are computed from `Trade.return_pct` -- a fraction of entry
price -- rather than a dollar amount. This is unconditionally true for
every `RiskMode.LEGACY_UNIT` trade (`DECISIONS.md` ADR-0011), which has
no persistent per-trade share count to derive a dollar P&L from without
inventing one; `return_pct` is the one per-trade number that already
exists and is unambiguous there. `total_pnl`/`total_return`, by
contrast, are read directly off the equity curve --
`BacktestResult.equity_curve` -- so those two are genuine dollar/
fractional totals for the whole run regardless of risk mode, not
per-trade constructs.

As of Sprint 11 (`DECISIONS.md`, ADR-0044), a `RiskMode.PORTFOLIO_RISK`
trade *does* carry a real `quantity`, so `Trade.gross_pnl` is a genuine
dollar figure per trade. Rather than reinterpret the existing
fraction-based functions above, this module adds explicit dollar-suffixed
siblings (`net_pnl_dollars`, `gross_profit_dollars`,
`gross_loss_dollars`, `expectancy_dollars`, `average_winner_dollars`,
`average_loser_dollars`, `largest_winner_dollars`,
`largest_loser_dollars`) gated by `has_quantity_detail()` -- the
fraction-based originals keep their exact existing behavior for every
caller and every historical experiment, and a caller must opt into the
dollar view explicitly rather than have `None` quantities silently
reinterpreted as zero-sized trades.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.analytics.models import Metric
from src.backtesting.metrics import infer_periods_per_year
from src.backtesting.models import Trade

DEFAULT_PERIODS_PER_YEAR = 252


@dataclass(frozen=True)
class AnnualizedMetric:
    """A `Metric` that was annualized, plus the exact factor used --
    the audit trail Sprint 9 section 8 requires for Sharpe (and, by the
    same reasoning, for volatility, which uses the same convention)."""

    metric: Metric
    periods_per_year: int | None


def total_pnl(equity_curve: pd.Series) -> Metric:
    """Dollar P&L over the whole run: last equity value minus first.

    `UNDEFINED` only for a genuinely empty equity curve -- a
    single-observation curve (e.g. a run that opened and closed nothing)
    yields a real, known answer of `0.0`, not an undefined one.
    """
    if equity_curve.empty:
        return Metric.undefined("no equity observations")
    return Metric.of(float(equity_curve.iloc[-1] - equity_curve.iloc[0]))


def total_return(equity_curve: pd.Series) -> Metric:
    """Fractional return over the whole run (e.g. 0.05 = +5%)."""
    if equity_curve.empty:
        return Metric.undefined("no equity observations")
    start = float(equity_curve.iloc[0])
    if start == 0:
        return Metric.undefined("equity curve starts at zero -- return is not defined")
    return Metric.of(float(equity_curve.iloc[-1]) / start - 1.0)


def win_rate(trades: list[Trade]) -> Metric:
    """Winning closed trades / total closed trades.

    Every `Trade` this platform's `Backtester` produces already
    represents a fully closed round-trip (entry price and exit price
    both set -- an in-progress position at the end of the data is
    closed at the final candle's price rather than left dangling, see
    `Trade.exit_signal_id`'s docstring), so there is no separate concept
    of an "open" trade to exclude here.
    """
    if not trades:
        return Metric.undefined("no closed trades")
    wins = sum(1 for t in trades if t.return_pct > 0)
    return Metric.of(wins / len(trades))


def trade_count(trades: list[Trade]) -> int:
    """Always defined, including `0` -- a count is a fact, never undefined."""
    return len(trades)


def winning_trade_count(trades: list[Trade]) -> int:
    return sum(1 for t in trades if t.return_pct > 0)


def losing_trade_count(trades: list[Trade]) -> int:
    return sum(1 for t in trades if t.return_pct < 0)


def profit_factor(trades: list[Trade]) -> Metric:
    """Gross profit / absolute gross loss, in return-fraction terms.

    `UNDEFINED` rather than a fabricated infinity when there are no
    losing trades to divide by (Sprint 9 spec, section 6's explicit
    requirement).
    """
    if not trades:
        return Metric.undefined("no closed trades")
    gross_profit = sum(t.return_pct for t in trades if t.return_pct > 0)
    gross_loss = abs(sum(t.return_pct for t in trades if t.return_pct < 0))
    if gross_loss == 0:
        return Metric.undefined(
            "no losing trades -- profit factor has no defined ceiling"
        )
    return Metric.of(gross_profit / gross_loss)


def expectancy(trades: list[Trade]) -> Metric:
    """Average return fraction per closed trade."""
    if not trades:
        return Metric.undefined("no closed trades")
    return Metric.of(sum(t.return_pct for t in trades) / len(trades))


def average_winner(trades: list[Trade]) -> Metric:
    winners = [t.return_pct for t in trades if t.return_pct > 0]
    if not winners:
        return Metric.undefined("no winning trades")
    return Metric.of(sum(winners) / len(winners))


def average_loser(trades: list[Trade]) -> Metric:
    losers = [t.return_pct for t in trades if t.return_pct < 0]
    if not losers:
        return Metric.undefined("no losing trades")
    return Metric.of(sum(losers) / len(losers))


def largest_winner(trades: list[Trade]) -> Metric:
    winners = [t.return_pct for t in trades if t.return_pct > 0]
    if not winners:
        return Metric.undefined("no winning trades")
    return Metric.of(max(winners))


def largest_loser(trades: list[Trade]) -> Metric:
    losers = [t.return_pct for t in trades if t.return_pct < 0]
    if not losers:
        return Metric.undefined("no losing trades")
    return Metric.of(min(losers))


def max_drawdown(equity_curve: pd.Series) -> Metric:
    """Largest peak-to-trough decline (a negative fraction), computed
    from the running peak of the equity curve itself -- never
    approximated from the final P&L alone (Sprint 9 spec, section 6).

    Deliberately `UNDEFINED` for an empty curve, unlike
    `src.backtesting.metrics.max_drawdown` (which returns `0.0` for
    that case for backward compatibility with the existing Backtester
    metrics dict) -- Sprint 9's stricter "don't fabricate" rule applies
    to this package's own results.

    Built on `drawdown_curve()` -- the single authoritative
    implementation of the drawdown formula in this package. A caller
    that needs the full series for charting (`src.dashboard`'s
    drawdown-curve visualization) calls `drawdown_curve()` directly
    rather than re-deriving it from `equity_curve` a second time --
    the dashboard must never calculate its own drawdown (Sprint 9
    spec, section 4).
    """
    if equity_curve.empty:
        return Metric.undefined("no equity observations")
    return Metric.of(float(drawdown_curve(equity_curve).min()))


def drawdown_curve(equity_curve: pd.Series) -> pd.Series:
    """The running drawdown at every point in `equity_curve` -- e.g.
    `-0.15` at a point 15% below the running peak so far.

    An empty `pd.Series` (never raising) for an empty `equity_curve`,
    matching this package's "empty in, empty out" convention elsewhere
    (`ExperimentRegistry.get_equity_curve()`).
    """
    if equity_curve.empty:
        return pd.Series(dtype=float, name="drawdown")
    running_max = equity_curve.cummax()
    return ((equity_curve - running_max) / running_max).rename("drawdown")


def _resolve_periods_per_year(
    period_returns: pd.Series, periods_per_year: int | None
) -> int:
    if periods_per_year is not None:
        return periods_per_year
    index = period_returns.index
    inferred = (
        infer_periods_per_year(index) if isinstance(index, pd.DatetimeIndex) else None
    )
    return inferred if inferred is not None else DEFAULT_PERIODS_PER_YEAR


def sharpe_ratio(
    equity_curve: pd.Series, periods_per_year: int | None = None
) -> AnnualizedMetric:
    """Annualized Sharpe ratio (zero risk-free rate), computed from
    periodic returns derived from `equity_curve` -- never from raw
    trade P&L (Sprint 9 spec, section 8).

    `periods_per_year` defaults to `None`, which infers the
    annualization factor from `equity_curve`'s own timestamp spacing
    (`infer_periods_per_year`) so a daily and a 1-minute backtest are
    never annualized by the same blind `sqrt(252)` (`DECISIONS.md`,
    ADR-0038). `UNDEFINED` (with no periods_per_year) for fewer than 2
    return observations or a zero-variance return series -- an
    undefined Sharpe is never reported as `0.0`.
    """
    period_returns = equity_curve.pct_change().dropna()
    if len(period_returns) < 2:
        return AnnualizedMetric(
            metric=Metric.undefined("fewer than 2 period returns"),
            periods_per_year=None,
        )
    std = period_returns.std(ddof=0)
    if std == 0:
        return AnnualizedMetric(
            metric=Metric.undefined(
                "zero variance in period returns -- Sharpe ratio is not defined"
            ),
            periods_per_year=None,
        )
    resolved = _resolve_periods_per_year(period_returns, periods_per_year)
    value = float((period_returns.mean() / std) * (resolved**0.5))
    return AnnualizedMetric(metric=Metric.of(value), periods_per_year=resolved)


def volatility(
    equity_curve: pd.Series, periods_per_year: int | None = None
) -> AnnualizedMetric:
    """Annualized standard deviation of periodic returns -- the same
    annualization convention as `sharpe_ratio` (not a separate,
    invented one), so the two stay directly comparable and both are
    auditable back to the same `periods_per_year`.
    """
    period_returns = equity_curve.pct_change().dropna()
    if len(period_returns) < 2:
        return AnnualizedMetric(
            metric=Metric.undefined("fewer than 2 period returns"),
            periods_per_year=None,
        )
    std = period_returns.std(ddof=0)
    resolved = _resolve_periods_per_year(period_returns, periods_per_year)
    return AnnualizedMetric(
        metric=Metric.of(float(std * (resolved**0.5))), periods_per_year=resolved
    )


def has_quantity_detail(trades: list[Trade]) -> bool:
    """Whether every trade in `trades` carries a real `quantity`
    (Sprint 11, `DECISIONS.md` ADR-0044) -- true only for a non-empty
    `RiskMode.PORTFOLIO_RISK` trade list. `False` for an empty list or
    any `RiskMode.LEGACY_UNIT` trade (where `quantity` is `None`), and
    also `False` for a mixed list -- this platform never actually
    produces one (a `BacktestResult`'s trades all come from the same
    risk mode), but the dollar-based functions below refuse to average
    dollar and fraction terms together rather than assume that can't
    happen.
    """
    return bool(trades) and all(t.quantity is not None for t in trades)


def net_pnl_dollars(trades: list[Trade]) -> Metric:
    """Sum of every trade's `gross_pnl` -- total dollar profit/loss for
    the run, before any costs (`DECISIONS.md`, ADR-0044). `UNDEFINED`
    when `trades` lacks quantity detail (a `RiskMode.LEGACY_UNIT` run,
    or no trades at all), never silently computed from `return_pct`
    instead -- that would mix fraction and dollar terms.
    """
    if not has_quantity_detail(trades):
        return Metric.undefined("no quantity detail -- trades were not sized (LEGACY_UNIT mode)")
    return Metric.of(sum(t.gross_pnl for t in trades))


def gross_profit_dollars(trades: list[Trade]) -> Metric:
    """Sum of `gross_pnl` across winning trades only."""
    if not has_quantity_detail(trades):
        return Metric.undefined("no quantity detail -- trades were not sized (LEGACY_UNIT mode)")
    return Metric.of(sum(t.gross_pnl for t in trades if t.gross_pnl > 0))


def gross_loss_dollars(trades: list[Trade]) -> Metric:
    """Absolute sum of `gross_pnl` across losing trades only."""
    if not has_quantity_detail(trades):
        return Metric.undefined("no quantity detail -- trades were not sized (LEGACY_UNIT mode)")
    return Metric.of(abs(sum(t.gross_pnl for t in trades if t.gross_pnl < 0)))


def expectancy_dollars(trades: list[Trade]) -> Metric:
    """Average `gross_pnl` per closed trade, in dollar terms -- the
    dollar analogue of `expectancy()` (which stays fraction-based for
    every trade list, quantity or not)."""
    if not has_quantity_detail(trades):
        return Metric.undefined("no quantity detail -- trades were not sized (LEGACY_UNIT mode)")
    return Metric.of(sum(t.gross_pnl for t in trades) / len(trades))


def average_winner_dollars(trades: list[Trade]) -> Metric:
    if not has_quantity_detail(trades):
        return Metric.undefined("no quantity detail -- trades were not sized (LEGACY_UNIT mode)")
    winners = [t.gross_pnl for t in trades if t.gross_pnl > 0]
    if not winners:
        return Metric.undefined("no winning trades")
    return Metric.of(sum(winners) / len(winners))


def average_loser_dollars(trades: list[Trade]) -> Metric:
    if not has_quantity_detail(trades):
        return Metric.undefined("no quantity detail -- trades were not sized (LEGACY_UNIT mode)")
    losers = [t.gross_pnl for t in trades if t.gross_pnl < 0]
    if not losers:
        return Metric.undefined("no losing trades")
    return Metric.of(sum(losers) / len(losers))


def largest_winner_dollars(trades: list[Trade]) -> Metric:
    if not has_quantity_detail(trades):
        return Metric.undefined("no quantity detail -- trades were not sized (LEGACY_UNIT mode)")
    winners = [t.gross_pnl for t in trades if t.gross_pnl > 0]
    if not winners:
        return Metric.undefined("no winning trades")
    return Metric.of(max(winners))


def largest_loser_dollars(trades: list[Trade]) -> Metric:
    if not has_quantity_detail(trades):
        return Metric.undefined("no quantity detail -- trades were not sized (LEGACY_UNIT mode)")
    losers = [t.gross_pnl for t in trades if t.gross_pnl < 0]
    if not losers:
        return Metric.undefined("no losing trades")
    return Metric.of(min(losers))


def exposure_time(trades: list[Trade], equity_curve: pd.Series) -> Metric:
    """Fraction of the run's elapsed time spent with an open position.

    `UNDEFINED` only when the equity curve itself has no defined
    duration (empty, or a single observation). No trades at all is a
    real, known answer of `0.0` -- zero time invested, not a missing
    value. Trades never overlap in this platform's backtester (one
    position at a time), so summing their durations directly is safe.
    """
    if equity_curve.empty or len(equity_curve) < 2:
        return Metric.undefined("no elapsed time in the equity curve")
    total_duration = equity_curve.index[-1] - equity_curve.index[0]
    if total_duration <= pd.Timedelta(0):
        return Metric.undefined("no elapsed time in the equity curve")
    if not trades:
        return Metric.of(0.0)
    curve_end = equity_curve.index[-1]
    invested_duration = pd.Timedelta(0)
    for trade in trades:
        invested_duration += min(trade.exit_time, curve_end) - trade.entry_time
    fraction = invested_duration / total_duration
    return Metric.of(max(0.0, min(1.0, float(fraction))))
