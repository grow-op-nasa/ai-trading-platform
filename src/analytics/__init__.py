"""Analytics: turning a backtest or experiment's raw trade and equity
data into decision-useful, deterministic metrics -- Sprint 9
(`DECISIONS.md`, ADR-0042).

`src.dashboard` is an interface over this package, not the other way
around: the dashboard must never calculate a metric itself
(`tests/test_architecture.py` enforces the dependency direction --
dashboard depends on analytics, never the reverse -- and confirms this
package has zero dependency on Streamlit).

    from src.analytics.service import AnalyticsService
    from src.experiments.registry import ExperimentRegistry

    registry = ExperimentRegistry()
    analytics = AnalyticsService().analyze_experiment(registry, experiment_id=3)
    print(analytics.sharpe_ratio, analytics.max_drawdown)

See `src.analytics.models` for the `Metric`/`BacktestAnalytics`/
`PortfolioSnapshot` shapes, `src.analytics.metrics` for the individual
deterministic calculations, `src.analytics.service` for the
`AnalyticsService` API and `compare_experiments()`, and
`src.analytics.valuation` for read-only, mark-to-market `Portfolio`
valuation.
"""
