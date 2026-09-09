"""Compiles a completed backtest into a deterministic set of
evidence-grounded findings.

This is the "what do we actually know" step, deliberately kept separate
from prose rendering (`renderers.py`) so it's fully testable without an
LLM, and so a renderer can never state a fact the compiler didn't
produce (`DECISIONS.md`, ADR-0020). Only reasons about evidence the
platform actually has today -- trade count, win rate, Sharpe, drawdown,
average hold, and regime (via `PerformanceAttributor`). No session-of-day
claim (e.g. "losses cluster after the open") is made here, since that
axis is deliberately unbuilt pending ADR-0006 (timezone consistency);
inventing one would violate the strict evidence-grounding this module
exists to guarantee.
"""

from __future__ import annotations

from src.attribution.models import AttributionReport
from src.backtesting.models import BacktestResult
from src.research.models import Finding, ResearchFindings
from src.utils.formatting import format_timedelta


def compile_findings(
    result: BacktestResult, attribution: AttributionReport
) -> ResearchFindings:
    """Extract `Finding`s from a completed backtest + attribution report.

    Args:
        result: a completed `Backtester.run()` result.
        attribution: `PerformanceAttributor.run(result, candles)`'s
            output for the same result.
    """
    findings: list[Finding] = [
        Finding("Strategy", result.strategy_name),
        Finding("Trades", str(attribution.total_trades)),
    ]

    if attribution.win_rate is not None:
        findings.append(Finding("Win Rate", f"{attribution.win_rate:.1%}"))

    sharpe = result.metrics.get("sharpe")
    if sharpe is not None:
        findings.append(Finding("Sharpe", f"{sharpe:.2f}"))

    max_drawdown = result.metrics.get("max_drawdown_pct")
    if max_drawdown is not None:
        findings.append(Finding("Max Drawdown", f"{max_drawdown:.2%}"))

    if attribution.average_hold is not None:
        findings.append(
            Finding("Average Hold", format_timedelta(attribution.average_hold))
        )

    if attribution.best_regime is not None:
        findings.append(Finding("Best Regime", attribution.best_regime))
    if attribution.worst_regime is not None:
        findings.append(Finding("Worst Regime", attribution.worst_regime))

    return ResearchFindings(
        strategy_name=result.strategy_name,
        findings=findings,
        recommendation=_next_experiment(attribution),
    )


def _next_experiment(attribution: AttributionReport) -> str | None:
    """A single, narrow suggestion for what to try next -- produced only
    when the evidence actually supports one.

    Only reasons about the regime axis, since that's the only breakdown
    Performance Attribution has today (session-of-day is deferred,
    ADR-0006/ADR-0019). Stays silent rather than guessing when there's
    no clear worst regime, or when the worst regime wasn't actually a
    loser on average.
    """
    if attribution.worst_regime is None or attribution.best_regime is None:
        return None
    if attribution.worst_regime == attribution.best_regime:
        return None

    worst_stats = next(
        (
            stats
            for stats in attribution.regime_breakdown.values()
            if stats.label == attribution.worst_regime
        ),
        None,
    )
    if (
        worst_stats is None
        or worst_stats.avg_return_pct is None
        or worst_stats.avg_return_pct >= 0
    ):
        return None

    return (
        f'Next experiment: investigate excluding or resizing trades entered '
        f'during "{attribution.worst_regime}" -- that regime accounted for '
        f"{worst_stats.trade_count} trade(s) with average return "
        f"{worst_stats.avg_return_pct:.2%}."
    )
