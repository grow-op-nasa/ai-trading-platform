"""NYSECalendar -- the first (and currently only) TradingCalendar.

Regular US equity session only (09:30-16:00 America/New_York), per the
Sprint 8 spec's explicit initial policy: "For the initial equity
research path, I'd make regular session only the default." Pre-market
and after-hours are a documented future capability, not built here
(`DECISIONS.md`, ADR-0041's non-goals).

Holiday sourcing (`DECISIONS.md`, ADR-0041): a small, explicit,
rule-based table rather than a hardcoded date list or a third-party
calendar-library dependency, per instruction not to build a full
exchange-calendar platform this round. NYSE's nine annual full-day
closures are each defined by a fixed calendar rule (e.g. "third Monday
in January"), so this computes them on demand for any year in
`SUPPORTED_YEARS` rather than enumerating specific dates -- correct for
every year in range without per-year maintenance, as long as NYSE's
observance rules themselves don't change (if they ever do, this is the
one place to update). `SUPPORTED_YEARS` is deliberately bounded and
explicit rather than "any year": a calendar silently answering for a
year nobody has actually verified this logic against is a worse
failure mode than a loud `ValueError`.

Known, documented limitation: this does not model NYSE's occasional
one-off *unscheduled* closures (e.g. a national day of mourning) --
those aren't representable by a fixed rule and would require an actual
maintained source of truth. Out of scope for this round.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from src.calendar.base import TradingCalendar

LOCAL_TIMEZONE = "America/New_York"
SESSION_OPEN_TIME = "09:30:00"
SESSION_CLOSE_TIME = "16:00:00"

# The range of years this calendar's holiday logic has actually been
# verified against. Extend deliberately (and re-verify), don't widen
# silently -- see module docstring.
SUPPORTED_YEARS = range(2015, 2036)


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """The date of the `n`-th `weekday` (Mon=0..Sun=6) in `year`-`month`."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """The date of the last `weekday` (Mon=0..Sun=6) in `year`-`month`."""
    next_month = date(year, month, 28) + timedelta(days=7)
    last_day_of_month = next_month - timedelta(days=next_month.day)
    offset = (last_day_of_month.weekday() - weekday) % 7
    return last_day_of_month - timedelta(days=offset)


def _easter_sunday(year: int) -> date:
    """Easter Sunday for `year`, via the Anonymous Gregorian algorithm.

    Standard, well-known algorithm for the Gregorian calendar (see e.g.
    Meeus, "Astronomical Algorithms") -- used here only to derive Good
    Friday (Easter Sunday minus two days), NYSE's one floating holiday
    that isn't a fixed "nth weekday of month" rule.
    """
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(holiday: date) -> date:
    """NYSE's weekend-observance shift: Saturday -> preceding Friday,
    Sunday -> following Monday. No shift for a weekday holiday."""
    if holiday.weekday() == 5:  # Saturday
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:  # Sunday
        return holiday + timedelta(days=1)
    return holiday


def _nyse_holidays(year: int) -> set[date]:
    """NYSE's full-day closures for `year`, observed-date adjusted."""
    holidays = {
        _observed(date(year, 1, 1)),  # New Year's Day
        _nth_weekday_of_month(year, 1, 0, 3),  # MLK Day: 3rd Monday, Jan
        _nth_weekday_of_month(year, 2, 0, 3),  # Presidents Day: 3rd Monday, Feb
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday_of_month(year, 5, 0),  # Memorial Day: last Monday, May
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday_of_month(year, 9, 0, 1),  # Labor Day: 1st Monday, Sep
        _nth_weekday_of_month(year, 11, 3, 4),  # Thanksgiving: 4th Thursday, Nov
        _observed(date(year, 12, 25)),  # Christmas
    }
    if year >= 2022:  # Juneteenth became an NYSE holiday starting 2022.
        holidays.add(_observed(date(year, 6, 19)))
    return holidays


class NYSECalendar(TradingCalendar):
    """Regular US equity session calendar: 09:30-16:00 America/New_York.

    Raises:
        ValueError: a method is called with a `date` (or a `date` at
            either end of a range) outside `SUPPORTED_YEARS`.
    """

    @property
    def local_timezone(self) -> str:
        return LOCAL_TIMEZONE

    def is_session(self, day: date) -> bool:
        self._check_supported(day)
        if day.weekday() >= 5:  # Saturday, Sunday
            return False
        return day not in _nyse_holidays(day.year)

    def session_open(self, day: date) -> pd.Timestamp:
        return self._session_instant(day, SESSION_OPEN_TIME)

    def session_close(self, day: date) -> pd.Timestamp:
        return self._session_instant(day, SESSION_CLOSE_TIME)

    def sessions_between(self, start: date, end: date) -> list[date]:
        self._check_supported(start)
        self._check_supported(end)
        return super().sessions_between(start, end)

    def _session_instant(self, day: date, time_str: str) -> pd.Timestamp:
        if not self.is_session(day):
            raise ValueError(f"{day} is not an NYSE trading day")
        local = pd.Timestamp(f"{day.isoformat()} {time_str}").tz_localize(LOCAL_TIMEZONE)
        return local.tz_convert("UTC")

    def _check_supported(self, day: date) -> None:
        if day.year not in SUPPORTED_YEARS:
            raise ValueError(
                f"NYSECalendar's holiday logic has only been verified for "
                f"{SUPPORTED_YEARS.start}-{SUPPORTED_YEARS.stop - 1}; "
                f"got {day} ({day.year})"
            )
