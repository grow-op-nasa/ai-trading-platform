"""Presentation-layer formatting -- src/dashboard/formatting.py.

Rounding and display formatting belong here, never in `src.analytics`
(Sprint 9 spec, section 10: the analytics engine preserves full numeric
precision; only the presentation layer rounds for display). Every
function in this module is a pure string formatter -- no Streamlit
import, no computation of a metric's *value*, only how an already-
computed `Metric` (or plain number) is shown. Deliberately importable
and testable without Streamlit installed at all.
"""

from __future__ import annotations

from src.analytics.models import Metric, MetricStatus


def format_number(value: float | None, kind: str = "ratio", digits: int = 2) -> str:
    """Render a plain number (not wrapped in a `Metric`) for display.

    Args:
        kind: `"pct"` (e.g. `12.34%`), `"dollar"` (e.g. `$1,234.56`),
            `"ratio"` (e.g. `1.23`), or `"count"` (a plain integer-
            looking number, comma-grouped).
    """
    if value is None:
        return "N/A"
    if kind == "pct":
        return f"{value * 100:.{digits}f}%"
    if kind == "dollar":
        sign = "-" if value < 0 else ""
        return f"{sign}${abs(value):,.{digits}f}"
    if kind == "count":
        return f"{value:,.0f}"
    return f"{value:.{digits}f}"


def format_metric(metric: Metric, kind: str = "ratio", digits: int = 2) -> str:
    """Render one `Metric` for display -- `"N/A -- <reason>"` when
    it's `UNDEFINED`, never a bare `0` or blank cell (Sprint 9 spec,
    section 9's "don't fabricate" rule extended to the presentation
    layer: an undefined metric must look visibly different from a
    computed zero)."""
    if metric.status is MetricStatus.UNDEFINED:
        return f"N/A -- {metric.reason}"
    return format_number(metric.value, kind=kind, digits=digits)


def format_interval(interval) -> str:
    """`interval.value` (e.g. `"1d"`) for a typed `Interval`, or
    `"unknown"` when `None` (an in-memory result analyzed without an
    `ExperimentSpec`)."""
    return interval.value if interval is not None else "unknown"


def format_optional(value: object, default: str = "unknown") -> str:
    return default if value is None else str(value)


def format_timestamp(ts: object) -> str:
    return "unknown" if ts is None else str(ts)


def format_hash(value: str | None, length: int = 12) -> str:
    """A truncated identity hash for compact display (e.g. a
    `dataset_fingerprint` or `strategy_version`) -- never the full
    hash, matching `scripts/run_experiment.py`'s own CLI output
    convention."""
    return "unknown" if not value else f"{value[:length]}..."
