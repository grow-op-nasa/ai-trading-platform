"""Streamlit view renderers -- src/dashboard/views.py.

Each `render_*` function is one page of the dashboard. Every number
shown is loaded via `src.analytics` (`AnalyticsService`,
`PortfolioValuationService`) or read directly off the
`ExperimentRegistry` -- this module never computes a Sharpe ratio, a
drawdown, a win rate, or a P&L itself (Sprint 9 spec, section 4: "the
dashboard is an interface over the platform; it is not the platform").
The one exception is `equity_curve - equity_curve.iloc[0]` for the
"Cumulative P&L" chart -- a presentation-only re-basing of a series
`src.analytics`/the registry already produced, not a new financial
calculation.

`st.cache_data` here is a presentation-layer optimization only (Sprint
9 spec, section 34: avoid re-reading every experiment file or
recalculating every metric per widget) -- never inside `src.analytics`
itself, which stays cache-free and pure.

Read-only throughout: no function in this module submits an order,
changes a risk limit, or mutates a `Portfolio`/`Experiment` record.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from loguru import logger

from src.analytics.metrics import drawdown_curve
from src.analytics.models import BacktestAnalytics, PortfolioSnapshot
from src.analytics.service import AnalyticsService, compare_experiments
from src.analytics.valuation import PortfolioValuationService, default_price_lookup
from src.dashboard.formatting import (
    format_hash,
    format_interval,
    format_metric,
    format_number,
    format_optional,
    format_timestamp,
)
from src.experiments.registry import ExperimentRegistry

# Sprint 9 spec, section 34: avoid recomputing full analytics for every
# experiment ever logged on every Overview render -- cap to the most
# recent handful. Not pagination (real future work if this becomes a
# problem in practice), just a guard against the obvious anti-pattern.
_OVERVIEW_ROW_LIMIT = 20


# ---------------------------------------------------------------------------
# Cached data loaders -- the presentation-layer caching boundary.
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30, show_spinner=False)
def _load_experiment_summaries(db_path: str) -> list[dict]:
    registry = ExperimentRegistry(db_path=db_path)
    experiments = registry.list_experiments()
    return [
        {
            "id": e.id,
            "strategy_name": e.strategy_name or "unknown",
            "created_at": e.created_at,
            "decision": e.decision,
        }
        for e in experiments
    ]


def list_experiment_ids(db_path: str) -> list[int]:
    """Experiment ids, most recently created first -- for selectors."""
    return [row["id"] for row in reversed(_load_experiment_summaries(db_path))]


@st.cache_data(ttl=30, show_spinner=False)
def _load_analytics(db_path: str, experiment_id: int) -> BacktestAnalytics | None:
    registry = ExperimentRegistry(db_path=db_path)
    return AnalyticsService().analyze_experiment(registry, experiment_id)


@st.cache_data(ttl=30, show_spinner=False)
def _load_equity_curve(db_path: str, experiment_id: int) -> pd.Series:
    registry = ExperimentRegistry(db_path=db_path)
    return registry.get_equity_curve(experiment_id)


@st.cache_data(ttl=30, show_spinner=False)
def _load_trades_frame(db_path: str, experiment_id: int) -> pd.DataFrame:
    registry = ExperimentRegistry(db_path=db_path)
    trades = registry.get_trades(experiment_id)
    columns = ["entry_time", "exit_time", "direction", "entry_price", "exit_price", "return_pct"]
    if not trades:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(
        {
            "entry_time": [t.entry_time for t in trades],
            "exit_time": [t.exit_time for t in trades],
            "direction": ["LONG" if t.direction > 0 else "SHORT" for t in trades],
            "entry_price": [t.entry_price for t in trades],
            "exit_price": [t.exit_price for t in trades],
            "return_pct": [t.return_pct for t in trades],
        }
    )


@st.cache_data(ttl=60, show_spinner=False)
def _cached_latest_price(symbol: str) -> float | None:
    # src.dashboard never constructs MarketDataService itself --
    # default_price_lookup() is the one call into src.analytics.valuation
    # that does, keeping the sanctioned chain (Dashboard -> Analytics/
    # Portfolio Valuation -> MarketDataService -> Canonical Data) intact
    # (Sprint 9 spec, section 23; tests/test_architecture.py enforces
    # this). Only ever called (via PortfolioValuationService.value())
    # once per symbol actually held open in a saved Portfolio -- never
    # for a portfolio with no open positions.
    return default_price_lookup()(symbol)


@st.cache_data(ttl=60, show_spinner=False)
def _value_portfolio(db_path: str, experiment_id: int) -> PortfolioSnapshot | None:
    registry = ExperimentRegistry(db_path=db_path)
    portfolio = registry.get_portfolio(experiment_id)
    if portfolio is None:
        return None
    return PortfolioValuationService().value(portfolio, price_lookup=_cached_latest_price)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def render_overview(db_path: str) -> None:
    st.header("Overview")
    summaries = _load_experiment_summaries(db_path)
    if not summaries:
        st.info("No experiments found yet -- run `python scripts/run_experiment.py` to create one.")
        return

    recent = list(reversed(summaries[-_OVERVIEW_ROW_LIMIT:]))
    rows = []
    for row in recent:
        analytics = _load_analytics(db_path, row["id"])
        rows.append(
            {
                "Experiment": row["id"],
                "Strategy": row["strategy_name"],
                "Created": row["created_at"],
                "Total return": format_metric(analytics.total_return, kind="pct") if analytics else "N/A",
                "Sharpe": format_metric(analytics.sharpe_ratio, kind="ratio") if analytics else "N/A",
                "Max drawdown": format_metric(analytics.max_drawdown, kind="pct") if analytics else "N/A",
                "Trades": analytics.trade_count if analytics else 0,
            }
        )
    st.subheader(f"Recent experiments (most recent {len(rows)} of {len(summaries)})")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    st.subheader("Current paper portfolio")
    most_recent_id = summaries[-1]["id"]
    st.caption(
        f"Experiment #{most_recent_id}'s own paper-executed portfolio -- the most "
        f"recently run experiment. Pick a different one on the Paper Portfolio page."
    )
    _render_portfolio_summary(db_path, most_recent_id)


def _render_portfolio_summary(db_path: str, experiment_id: int) -> None:
    """A compact 4-metric summary shared by the Overview page -- the
    full Paper Portfolio page (`render_paper_portfolio`) adds the
    per-position breakdown on top of this."""
    registry = ExperimentRegistry(db_path=db_path)
    if registry.get_portfolio(experiment_id) is None:
        st.info("No paper portfolio saved for this experiment.")
        return

    snapshot = _safe_value_portfolio(db_path, experiment_id)
    if snapshot is None:
        return

    columns = st.columns(4)
    columns[0].metric("Cash", format_number(snapshot.cash, "dollar"))
    columns[1].metric("Equity", format_metric(snapshot.equity, kind="dollar"))
    columns[2].metric("Unrealized P&L", format_metric(snapshot.unrealized_pnl, kind="dollar"))
    columns[3].metric("Realized P&L", format_number(snapshot.realized_pnl, "dollar"))
    for warning in snapshot.warnings:
        st.warning(warning)


def _safe_value_portfolio(db_path: str, experiment_id: int) -> PortfolioSnapshot | None:
    """`_value_portfolio`, with any unexpected failure (a market-data
    outage, a malformed price response) turned into a friendly message
    instead of a raw traceback -- logged for developers via loguru,
    never hidden entirely (Sprint 9 spec's error-handling requirement).
    Per-symbol "price unavailable" cases are already handled inside
    `PortfolioValuationService`/`latest_price` and never reach here as
    an exception; this only catches the genuinely unexpected.
    """
    try:
        return _value_portfolio(db_path, experiment_id)
    except Exception:
        logger.exception(f"Failed to value the paper portfolio for experiment #{experiment_id}")
        st.error("Paper portfolio unavailable -- could not compute a valuation right now.")
        return None


# ---------------------------------------------------------------------------
# Backtest / Experiment Analysis
# ---------------------------------------------------------------------------


def render_experiment_analysis(db_path: str, experiment_id: int) -> None:
    st.header(f"Backtest / Experiment Analysis -- #{experiment_id}")
    analytics = _load_analytics(db_path, experiment_id)
    if analytics is None:
        st.error(f"Experiment #{experiment_id} was not found.")
        return

    _render_identity(analytics)

    if not analytics.has_equity_curve:
        st.warning("No equity curve available for this experiment.")
    if not analytics.has_trade_detail:
        st.warning("No closed trades recorded for this experiment.")

    row1 = st.columns(4)
    row1[0].metric("Total return", format_metric(analytics.total_return, kind="pct"))
    row1[1].metric("Sharpe ratio", format_metric(analytics.sharpe_ratio, kind="ratio"))
    row1[2].metric("Max drawdown", format_metric(analytics.max_drawdown, kind="pct"))
    row1[3].metric("Win rate", format_metric(analytics.win_rate, kind="pct"))

    row2 = st.columns(4)
    row2[0].metric("Total P&L", format_metric(analytics.total_pnl, kind="dollar"))
    row2[1].metric("Profit factor", format_metric(analytics.profit_factor, kind="ratio"))
    row2[2].metric("Expectancy", format_metric(analytics.expectancy, kind="pct"))
    row2[3].metric("Trades", format_number(float(analytics.trade_count), kind="count"))

    with st.expander("More metrics"):
        row3 = st.columns(4)
        row3[0].metric("Avg winner", format_metric(analytics.average_winner, kind="pct"))
        row3[1].metric("Avg loser", format_metric(analytics.average_loser, kind="pct"))
        row3[2].metric("Largest winner", format_metric(analytics.largest_winner, kind="pct"))
        row3[3].metric("Largest loser", format_metric(analytics.largest_loser, kind="pct"))
        row4 = st.columns(4)
        row4[0].metric("Winning trades", str(analytics.winning_trade_count))
        row4[1].metric("Losing trades", str(analytics.losing_trade_count))
        row4[2].metric("Volatility (ann.)", format_metric(analytics.volatility, kind="pct"))
        row4[3].metric("Exposure time", format_metric(analytics.exposure_time, kind="pct"))

    if analytics.sharpe_periodicity_note:
        st.caption(analytics.sharpe_periodicity_note)

    equity_curve = _load_equity_curve(db_path, experiment_id)
    if not equity_curve.empty:
        st.subheader("Equity curve")
        st.line_chart(equity_curve)

        st.subheader("Drawdown")
        st.line_chart(drawdown_curve(equity_curve))

        st.subheader("Cumulative P&L")
        st.line_chart(equity_curve - equity_curve.iloc[0])

    trades_frame = _load_trades_frame(db_path, experiment_id)
    if not trades_frame.empty:
        st.subheader("Trade P&L distribution")
        st.bar_chart(trades_frame["return_pct"])

        st.subheader("Trades")
        st.dataframe(trades_frame, hide_index=True, use_container_width=True)

    _render_research_report(db_path, experiment_id)


def _render_identity(analytics: BacktestAnalytics) -> None:
    columns = st.columns(4)
    columns[0].write(f"**Strategy:** {format_optional(analytics.strategy_name)}")
    columns[1].write(f"**Symbol:** {format_optional(analytics.symbol)}")
    columns[2].write(f"**Interval:** {format_interval(analytics.interval)}")
    columns[3].write(f"**Dataset:** {format_hash(analytics.dataset_fingerprint)}")
    st.caption(
        f"Strategy version: {format_hash(analytics.strategy_version)} -- dataset "
        f"{format_timestamp(analytics.dataset_start)} to {format_timestamp(analytics.dataset_end)}"
    )


def _render_research_report(db_path: str, experiment_id: int) -> None:
    registry = ExperimentRegistry(db_path=db_path)
    report = registry.get_research_report(experiment_id)
    if report is None:
        return
    st.subheader("Research report")
    if report.renderer_error:
        st.caption(
            f"Rendered by: {report.rendered_by} (LLM renderer unavailable at run "
            f"time -- {report.renderer_error}; the deterministic fallback below was used)"
        )
    else:
        st.caption(f"Rendered by: {report.rendered_by}")
    st.write(report.narrative)


# ---------------------------------------------------------------------------
# Strategy / Experiment Comparison
# ---------------------------------------------------------------------------


def render_comparison(db_path: str, experiment_ids: list[int]) -> None:
    st.header("Strategy / Experiment Comparison")
    if len(experiment_ids) < 2:
        st.info("Select two or more experiments to compare.")
        return

    analytics_list = []
    for experiment_id in experiment_ids:
        analytics = _load_analytics(db_path, experiment_id)
        if analytics is None:
            st.warning(f"Experiment #{experiment_id} was not found and is excluded from the comparison.")
        else:
            analytics_list.append(analytics)

    if len(analytics_list) < 2:
        st.info("Need at least two valid experiments to compare.")
        return

    comparison = compare_experiments(analytics_list)
    for warning in comparison.warnings:
        st.warning(
            f"These experiments differ in **{warning.field}**: {', '.join(warning.values)} -- "
            f"comparing them directly may not be meaningful."
        )

    table = pd.DataFrame(
        [
            {
                "Experiment": a.experiment_id,
                "Strategy": format_optional(a.strategy_name),
                "Symbol": format_optional(a.symbol),
                "Interval": format_interval(a.interval),
                "Total return": format_metric(a.total_return, kind="pct"),
                "Net P&L": format_metric(a.total_pnl, kind="dollar"),
                "Sharpe": format_metric(a.sharpe_ratio, kind="ratio"),
                "Max drawdown": format_metric(a.max_drawdown, kind="pct"),
                "Win rate": format_metric(a.win_rate, kind="pct"),
                "Profit factor": format_metric(a.profit_factor, kind="ratio"),
                "Expectancy": format_metric(a.expectancy, kind="pct"),
                "Trades": a.trade_count,
            }
            for a in comparison.rows
        ]
    )
    st.dataframe(table, hide_index=True, use_container_width=True)

    st.subheader("Equity comparison")
    st.caption(
        "Aligned by bar number, not calendar date -- compared experiments may span "
        "different date ranges or timeframes (see any warning above)."
    )
    curves = {}
    for a in comparison.rows:
        curve = _load_equity_curve(db_path, a.experiment_id)
        if not curve.empty:
            label = f"#{a.experiment_id} {format_optional(a.strategy_name)}"
            curves[label] = curve.reset_index(drop=True)
    if curves:
        st.line_chart(pd.DataFrame(curves))
    else:
        st.info("No equity curves available for the selected experiments.")


# ---------------------------------------------------------------------------
# Paper Portfolio
# ---------------------------------------------------------------------------


def render_paper_portfolio(db_path: str, experiment_id: int) -> None:
    st.header(f"Paper Portfolio -- Experiment #{experiment_id}")
    registry = ExperimentRegistry(db_path=db_path)
    if registry.get_portfolio(experiment_id) is None:
        st.info("No paper portfolio was saved for this experiment.")
        return

    snapshot = _safe_value_portfolio(db_path, experiment_id)
    if snapshot is None:
        return

    row1 = st.columns(4)
    row1[0].metric("Cash", format_number(snapshot.cash, "dollar"))
    row1[1].metric("Equity", format_metric(snapshot.equity, kind="dollar"))
    row1[2].metric("Unrealized P&L", format_metric(snapshot.unrealized_pnl, kind="dollar"))
    row1[3].metric("Realized P&L", format_number(snapshot.realized_pnl, "dollar"))

    row2 = st.columns(2)
    row2[0].metric("Market value", format_metric(snapshot.market_value, kind="dollar"))
    row2[1].metric("Exposure", format_metric(snapshot.exposure, kind="dollar"))

    for warning in snapshot.warnings:
        st.warning(warning)

    if snapshot.positions:
        st.subheader("Open positions")
        positions_frame = pd.DataFrame(
            [
                {
                    "Symbol": p.symbol,
                    "Side": p.side,
                    "Quantity": p.quantity,
                    "Entry price": p.entry_price,
                    "Market price": p.market_price if p.price_available else "N/A",
                    "Market value": p.market_value if p.price_available else "N/A",
                    "Unrealized P&L": p.unrealized_pnl if p.price_available else "N/A",
                }
                for p in snapshot.positions
            ]
        )
        st.dataframe(positions_frame, hide_index=True, use_container_width=True)
    else:
        st.info("No open positions.")

    st.caption(
        "Reflects this experiment's own paper-executed portfolio (Risk -> Execution -> "
        "Portfolio), marked to the latest available market price via MarketDataService. "
        "Read-only -- viewing this page never places an order or changes portfolio state. "
        "Real broker accounts are out of scope this sprint."
    )
