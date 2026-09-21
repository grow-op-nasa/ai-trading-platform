"""The Analytics application service -- src/analytics/service.py.

`AnalyticsService` is the one thing a caller (a test, a future CLI, or
`src.dashboard`) actually talks to -- it never manually invokes
individual `src.analytics.metrics` functions itself and stitches the
result together by hand (Sprint 9 spec, section 25). Two entry points:

    AnalyticsService().analyze_backtest(result, spec=spec)
    AnalyticsService().analyze_experiment(registry, experiment_id)

`compare_experiments()` builds a side-by-side `ComparisonResult` from
already-computed `BacktestAnalytics` rows -- it never computes a
composite "best strategy" score (section 15); it only flags material
differences (symbol, timeframe, dataset, strategy implementation) that
would make a naive comparison misleading (section 43).
"""

from __future__ import annotations

import pandas as pd

from src.analytics import metrics as m
from src.analytics.models import BacktestAnalytics, ComparisonResult, ComparisonWarning
from src.backtesting.models import BacktestResult, Trade
from src.experiments.registry import ExperimentRegistry
from src.experiments.spec import ExperimentSpec

_COMPARISON_FIELDS = ("symbol", "interval", "dataset_fingerprint", "strategy_version")


class AnalyticsService:
    """Computes a `BacktestAnalytics` from either an in-memory
    `BacktestResult` or a past experiment loaded from the
    `ExperimentRegistry` -- the single place Sprint 9's metric
    definitions (`src.analytics.metrics`) are actually wired together.
    """

    def analyze_backtest(
        self,
        result: BacktestResult,
        spec: ExperimentSpec | None = None,
        experiment_id: int | None = None,
    ) -> BacktestAnalytics:
        """Analyze a `BacktestResult` directly -- for a backtest just
        run in this same process, before (or without) ever being
        persisted to the `ExperimentRegistry`. `spec`/`experiment_id`
        are optional identity context; omitting them simply leaves the
        corresponding `BacktestAnalytics` identity fields `None`.
        """
        return self._analyze(
            trades=result.trades,
            equity_curve=result.equity_curve,
            spec=spec,
            experiment_id=experiment_id,
        )

    def analyze_experiment(
        self, registry: ExperimentRegistry, experiment_id: int
    ) -> BacktestAnalytics | None:
        """Load and analyze a past experiment purely from what
        `scripts/run_experiment.py` persisted (Sprint 9, `DECISIONS.md`
        ADR-0042) -- works for any experiment logged since Sprint 9
        without that experiment still being in memory anywhere.

        Returns `None` if `experiment_id` doesn't exist at all
        (`registry.get_experiment()` returns `None`). An experiment
        that exists but predates Sprint 9's trade/equity persistence
        still analyzes -- `registry.get_trades()`/`get_equity_curve()`
        both return an empty result rather than raising, so every
        metric that needs them reports `UNDEFINED` (never a fabricated
        number) instead of the whole call failing (section 19: degrade
        gracefully, don't require the newer data to exist).
        """
        experiment = registry.get_experiment(experiment_id)
        if experiment is None:
            return None
        trades = registry.get_trades(experiment_id)
        equity_curve = registry.get_equity_curve(experiment_id)
        spec = registry.get_spec(experiment_id)
        return self._analyze(
            trades=trades,
            equity_curve=equity_curve,
            spec=spec,
            experiment_id=experiment_id,
        )

    def _analyze(
        self,
        *,
        trades: list[Trade],
        equity_curve: pd.Series,
        spec: ExperimentSpec | None,
        experiment_id: int | None,
    ) -> BacktestAnalytics:
        sharpe = m.sharpe_ratio(equity_curve)
        vol = m.volatility(equity_curve)

        return BacktestAnalytics(
            experiment_id=experiment_id,
            strategy_name=spec.strategy_name if spec else None,
            strategy_version=spec.strategy_version if spec else None,
            symbol=spec.symbol if spec else None,
            interval=spec.interval if spec else None,
            dataset_fingerprint=spec.dataset_fingerprint if spec else None,
            dataset_start=spec.dataset_start if spec else None,
            dataset_end=spec.dataset_end if spec else None,
            total_pnl=m.total_pnl(equity_curve),
            total_return=m.total_return(equity_curve),
            sharpe_ratio=sharpe.metric,
            max_drawdown=m.max_drawdown(equity_curve),
            win_rate=m.win_rate(trades),
            trade_count=m.trade_count(trades),
            profit_factor=m.profit_factor(trades),
            expectancy=m.expectancy(trades),
            average_winner=m.average_winner(trades),
            average_loser=m.average_loser(trades),
            largest_winner=m.largest_winner(trades),
            largest_loser=m.largest_loser(trades),
            winning_trade_count=m.winning_trade_count(trades),
            losing_trade_count=m.losing_trade_count(trades),
            volatility=vol.metric,
            exposure_time=m.exposure_time(trades, equity_curve),
            sharpe_periods_per_year=sharpe.periods_per_year,
            sharpe_periodicity_note=_periodicity_note(sharpe.periods_per_year),
            has_trade_detail=bool(trades),
            has_equity_curve=not equity_curve.empty,
        )


def _periodicity_note(periods_per_year: int | None) -> str | None:
    if periods_per_year is None:
        return None
    return (
        f"Annualized using {periods_per_year} periods/year, inferred from "
        f"the equity curve's own timestamp spacing (DECISIONS.md, ADR-0038)."
    )


def compare_experiments(analytics_list: list[BacktestAnalytics]) -> ComparisonResult:
    """Build a side-by-side comparison of already-computed
    `BacktestAnalytics` rows.

    Never computes a composite "best strategy" score or ranking
    (Sprint 9 spec, section 15) -- `rows` are returned exactly as
    given, in the order provided. Flags (never blocks) a material
    difference in symbol, interval, dataset fingerprint, or strategy
    implementation hash across the rows being compared (section 43):
    comparing a 1-minute QQQ run against a 1-day SPY run is allowed,
    but never silently presented as apples-to-apples.
    """
    warnings: list[ComparisonWarning] = []
    for field_name in _COMPARISON_FIELDS:
        values = tuple(_display(getattr(a, field_name)) for a in analytics_list)
        if len(set(values)) > 1:
            warnings.append(ComparisonWarning(field=field_name, values=values))
    return ComparisonResult(rows=list(analytics_list), warnings=warnings)


def _display(value: object) -> str:
    return "unknown" if value is None else str(value)
