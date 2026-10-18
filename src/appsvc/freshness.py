"""Data-freshness context (Sprint 20). Calendar arithmetic over STORED records only -- no market-data request is made.

It answers two honest questions: what is the most recent session that has closed (per the NYSE calendar the runtime
uses), and how does the latest RECORDED decision bar compare. It never claims the provider currently has a bar; that is
only learned by an explicit preflight or run, which checks the real data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import pandas as pd


@dataclass(frozen=True)
class Freshness:
    state: str                       # NONE | CURRENT | NEWER_SESSION | UNKNOWN
    expected_session: date | None
    last_bar_session: date | None
    message: str
    next_step: str = ""


def expected_session(now: datetime | None = None, calendar=None) -> date | None:
    try:
        from src.calendar.nyse import NYSECalendar
        from src.paper import bars
        return bars.expected_completed_session(calendar or NYSECalendar(), now or datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 -- an unsupported calendar range is "unknown", never a guess
        return None


def for_cycle(decision_bar: str | None, now: datetime | None = None, calendar=None) -> Freshness:
    exp = expected_session(now, calendar)
    if not decision_bar:
        return Freshness("NONE", exp, None, "No decision bar has been recorded for this account yet.",
                         "Run a paper cycle to evaluate the latest completed session.")
    last = pd.Timestamp(decision_bar).tz_convert("UTC").date()
    if exp is None:
        return Freshness("UNKNOWN", None, last, f"The latest recorded decision bar is for session {last}; the expected session could not be determined.")
    if last >= exp:
        return Freshness("CURRENT", exp, last, f"The latest recorded decision bar ({last}) is the most recent completed session. "
                         "This is the recorded state, not a live data check.", "")
    return Freshness("NEWER_SESSION", exp, last, f"The latest recorded decision bar is {last}, but session {exp} has since closed. "
                     "A new cycle can evaluate it (the provider may need a little time to publish it).", "Run a paper cycle.")
