"""Tests for `src.dashboard.formatting` -- Sprint 9 (`DECISIONS.md`,
ADR-0042).

Deliberately unconditional (no `pytest.importorskip("streamlit")`):
`src.dashboard.formatting` has zero Streamlit dependency by design --
it's pure string formatting -- so it's fully testable even where
Streamlit itself isn't installed (this sandbox included). The rest of
`src.dashboard` (`views.py`, `app.py`) is covered by
`tests/test_dashboard_smoke.py`, which does need the real Streamlit
testing facility and skips where it's unavailable.
"""

from __future__ import annotations

from src.analytics.models import Metric
from src.dashboard.formatting import (
    format_hash,
    format_interval,
    format_metric,
    format_number,
    format_optional,
    format_timestamp,
)
from src.data.base import Interval


def test_format_number_none_is_na():
    assert format_number(None) == "N/A"


def test_format_number_pct():
    assert format_number(0.1234, kind="pct") == "12.34%"


def test_format_number_pct_negative():
    assert format_number(-0.05, kind="pct") == "-5.00%"


def test_format_number_dollar_positive():
    assert format_number(1234.5, kind="dollar") == "$1,234.50"


def test_format_number_dollar_negative_sign_before_symbol():
    assert format_number(-1234.5, kind="dollar") == "-$1,234.50"


def test_format_number_count():
    assert format_number(1234.0, kind="count") == "1,234"


def test_format_number_ratio_default():
    assert format_number(1.23456) == "1.23"


def test_format_metric_ok_delegates_to_format_number():
    metric = Metric.of(0.5)
    assert format_metric(metric, kind="pct") == "50.00%"


def test_format_metric_undefined_shows_reason():
    metric = Metric.undefined("no closed trades")
    assert format_metric(metric) == "N/A -- no closed trades"


def test_format_interval_known_value():
    assert format_interval(Interval.DAY_1) == "1d"


def test_format_interval_none_is_unknown():
    assert format_interval(None) == "unknown"


def test_format_optional_none_uses_default():
    assert format_optional(None) == "unknown"
    assert format_optional(None, default="n/a") == "n/a"


def test_format_optional_present_value():
    assert format_optional("ema_cross") == "ema_cross"


def test_format_timestamp_none_is_unknown():
    assert format_timestamp(None) == "unknown"


def test_format_hash_truncates():
    assert format_hash("abcdef0123456789", length=6) == "abcdef..."


def test_format_hash_none_or_empty_is_unknown():
    assert format_hash(None) == "unknown"
    assert format_hash("") == "unknown"
