"""The Trading Calendar capability -- src/calendar/.

Sprint 8 (`DECISIONS.md`, ADR-0041, "Market Data Integrity & Session
Awareness"): a market-session abstraction so NYSE-hours assumptions
(09:30-16:00 America/New_York, weekends, holidays) live in exactly one
place, rather than being scattered through `src/data`'s validation and
gap-detection logic, the strategy engine, or the backtester.

    from src.calendar import NYSECalendar

    calendar = NYSECalendar()
    calendar.is_session(date(2024, 7, 4))       # False -- holiday
    calendar.session_open(date(2024, 1, 2))     # Timestamp, UTC

Named to mirror `src/broker`, `src/risk`, `src/portfolio`, etc. --
one top-level package per capability. Despite the name, this is a
sibling of the standard library's `calendar` module, not a replacement
for it: Python's absolute-import default means `import calendar`
anywhere in this codebase still resolves to the standard library: only
an explicit `from src.calendar import ...` reaches this package, so the
two never collide.
"""

from __future__ import annotations

from src.calendar.base import TradingCalendar
from src.calendar.nyse import NYSECalendar

__all__ = ["TradingCalendar", "NYSECalendar"]
