"""Data shapes produced by the Analytics layer -- src/analytics/models.py.

Sprint 9 (`DECISIONS.md`, ADR-0042): plain dataclasses, no behavior
beyond simple derived properties -- `src.analytics.metrics` and
`src.analytics.service`/`src.analytics.valuation` do the actual
computation. No dependency on Streamlit anywhere in this package
(`tests/test_architecture.py` enforces it) -- these results must be
equally usable from a CLI, a test, or the dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

from src.data.base import Interval


class MetricStatus(str, Enum):
    """Whether a `Metric`'s `value` is meaningful.

    `OK` -- `value` is a real, computed number.
    `UNDEFINED` -- the underlying mathematics has no defined answer
    (no trades, zero variance, no equity observations, ...) -- `value`
    is `None`, and `reason` says why. Never silently coerced to `0` or
    `inf` (Sprint 9 spec, section 9).
    """

    OK = "ok"
    UNDEFINED = "undefined"


@dataclass(frozen=True)
class Metric:
    """One analytics number, or an explicit statement that it doesn't
    exist -- the uniform representation every metric in this package
    returns, so a caller never has to guess whether `None` means "zero"
    or "not computed" (Sprint 9 spec, section 9).

    Args:
        value: the computed number, full precision, no rounding
            (Sprint 9 spec, section 10: rounding/formatting belongs at
            the presentation layer, never here). `None` when `status`
            is `UNDEFINED`.
        status: `MetricStatus.OK` or `MetricStatus.UNDEFINED`.
        reason: a short, human-readable explanation. Set when `status`
            is `UNDEFINED`; always `None` when `status` is `OK`.
    """

    value: float | None
    status: MetricStatus
    reason: str | None = None

    @classmethod
    def of(cls, value: float) -> "Metric":
        """An `OK` metric wrapping `value`."""
        return cls(value=value, status=MetricStatus.OK)

    @classmethod
    def undefined(cls, reason: str) -> "Metric":
        """An `UNDEFINED` metric with `reason` explaining why."""
        return cls(value=None, status=MetricStatus.UNDEFINED, reason=reason)

    @property
    def is_defined(self) -> bool:
        return self.status is MetricStatus.OK


@dataclass
class BacktestAnalytics:
    """The full analytics result for one backtest/experiment (Sprint 9
    spec, section 26 -- "AnalyticsResult").

    Identity fields travel with the metrics, never separately, so a
    dashboard or comparison can never present two materially different
    experiments as if they were the same thing (Sprint 9 spec, sections
    13/42). Each identity field is `None` when it genuinely isn't known
    -- e.g. a fresh in-memory `BacktestResult` analyzed before any
    `ExperimentSpec` exists -- rather than a guessed placeholder.
    """

    # Identity / provenance (sections 13, 42).
    experiment_id: int | None
    strategy_name: str | None
    strategy_version: str | None
    symbol: str | None
    interval: Interval | None
    dataset_fingerprint: str | None
    dataset_start: pd.Timestamp | None
    dataset_end: pd.Timestamp | None

    # Core metrics (section 6).
    total_pnl: Metric
    total_return: Metric
    sharpe_ratio: Metric
    max_drawdown: Metric
    win_rate: Metric
    trade_count: int

    # Additional metrics (section 6), where the data supports them cleanly.
    profit_factor: Metric
    expectancy: Metric
    average_winner: Metric
    average_loser: Metric
    largest_winner: Metric
    largest_loser: Metric
    winning_trade_count: int
    losing_trade_count: int
    volatility: Metric
    exposure_time: Metric

    # Sharpe (and volatility) auditability (section 8): every displayed
    # value should be traceable back to periodicity + annualization
    # convention, not a black-box number.
    sharpe_periods_per_year: int | None
    sharpe_periodicity_note: str | None

    # Data completeness: did this result come from a full persisted
    # backtest (trades + equity curve), or degrade to whatever a
    # pre-Sprint-9 experiment happens to have on file (section 19,
    # "where the data supports it cleanly")?
    has_trade_detail: bool
    has_equity_curve: bool

    # Dollar-denominated per-trade metrics (Sprint 11, `DECISIONS.md`
    # ADR-0044) -- the dollar analogues of `expectancy`/`average_winner`/
    # `average_loser`/`largest_winner`/`largest_loser` above, populated
    # only when every trade carries a real `Trade.quantity` (a
    # `RiskMode.PORTFOLIO_RISK` result). `has_quantity_detail` says
    # explicitly whether these are populated -- never inferred by
    # checking if a `Metric` happens to be `UNDEFINED`, since an
    # `UNDEFINED` dollar metric can also mean "no winning trades" on a
    # run that *does* have quantity detail. The fraction-based fields
    # above are always populated regardless of risk mode; these are
    # additive, never a replacement for them.
    has_quantity_detail: bool
    net_pnl_dollars: Metric
    gross_profit_dollars: Metric
    gross_loss_dollars: Metric
    expectancy_dollars: Metric
    average_winner_dollars: Metric
    average_loser_dollars: Metric
    largest_winner_dollars: Metric
    largest_loser_dollars: Metric

    # Execution-cost metrics (Sprint 12, `DECISIONS.md` ADR-0045) --
    # how much of the dollar P&L above was consumed by simulated
    # execution friction. `has_execution_cost_detail` is `True` only
    # when every trade has a real fill price for both legs (an
    # `ExecutionModel` actually ran); `total_fees_dollars` alone can
    # still be defined even when that's `False`, since fees default to
    # `0.0` per trade regardless (see `has_execution_cost_detail`'s own
    # docstring in `src.analytics.metrics`).
    has_execution_cost_detail: bool
    total_fees_dollars: Metric
    total_slippage_cost_dollars: Metric
    net_pnl_after_costs_dollars: Metric


@dataclass(frozen=True)
class ComparisonWarning:
    """One flagged material difference across a set of compared
    experiments (Sprint 9 spec, section 43) -- e.g. comparing a 1-minute
    QQQ run against a 1-day SPY run must never be silently presented as
    apples-to-apples."""

    field: str
    values: tuple[str, ...]


@dataclass
class ComparisonResult:
    """A side-by-side view of already-computed `BacktestAnalytics` rows.

    Deliberately carries no composite "best strategy" score or ranking
    (Sprint 9 spec, section 15) -- `rows` is presented as-is, with
    `warnings` flagging anything that would make a naive comparison
    misleading.
    """

    rows: list[BacktestAnalytics]
    warnings: list[ComparisonWarning] = field(default_factory=list)


@dataclass(frozen=True)
class PositionValuation:
    """One open position's mark-to-market valuation, or an explicit
    statement that no current price was available for it (Sprint 9
    spec, section 22: never fabricate a price)."""

    symbol: str
    side: str
    quantity: float
    entry_price: float
    market_price: float | None
    market_value: float | None
    unrealized_pnl: float | None
    price_available: bool
    warning: str | None = None


@dataclass
class PortfolioSnapshot:
    """A read-only, mark-to-market view of a `Portfolio` at the moment
    it was valued (Sprint 9 spec, sections 20-24).

    `cash` and `realized_pnl` are always known exactly -- neither
    depends on a current market price. `market_value`, `unrealized_pnl`,
    `total_pnl`, `equity`, and `exposure` are each an `UNDEFINED` Metric
    whenever *any* open position lacks a current price: a partial sum
    that silently omits an unpriced position would itself be a
    misleading number (the same "don't fabricate" principle applied to
    aggregates, not just individual positions). `positions` always shows
    the full breakdown, including exactly which symbols were priced.
    """

    cash: float
    market_value: Metric
    realized_pnl: float
    unrealized_pnl: Metric
    total_pnl: Metric
    equity: Metric
    exposure: Metric
    positions: list[PositionValuation]
    warnings: list[str] = field(default_factory=list)
