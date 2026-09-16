"""Tests for the Trading Calendar capability (`DECISIONS.md`, ADR-0041).

`NYSECalendar` is the only concrete `TradingCalendar` implementation --
these tests cover its holiday-rule correctness (the part most likely to
have an off-by-one), its regular-session open/close conversion to UTC,
and the base class's shared `sessions_between`/`session_date` helpers.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.calendar.base import TradingCalendar
from src.calendar.nyse import NYSECalendar


@pytest.fixture
def calendar() -> NYSECalendar:
    return NYSECalendar()


# -- is_session: weekends -----------------------------------------------


def test_weekday_is_a_session(calendar):
    assert calendar.is_session(date(2024, 1, 2))  # Tuesday


def test_saturday_is_not_a_session(calendar):
    assert not calendar.is_session(date(2024, 1, 6))


def test_sunday_is_not_a_session(calendar):
    assert not calendar.is_session(date(2024, 1, 7))


# -- is_session: fixed-rule holidays (2024) ------------------------------


def test_known_2024_nyse_holidays_are_not_sessions(calendar):
    # A plain loop, not pytest.mark.parametrize -- this codebase's own
    # sandbox test runner doesn't support parametrize (see other test
    # files' convention of looping over cases directly).
    known_2024_holidays = [
        date(2024, 1, 1),  # New Year's Day
        date(2024, 1, 15),  # MLK Day
        date(2024, 2, 19),  # Presidents Day
        date(2024, 3, 29),  # Good Friday
        date(2024, 5, 27),  # Memorial Day
        date(2024, 6, 19),  # Juneteenth
        date(2024, 7, 4),  # Independence Day
        date(2024, 9, 2),  # Labor Day
        date(2024, 11, 28),  # Thanksgiving
        date(2024, 12, 25),  # Christmas
    ]
    for holiday in known_2024_holidays:
        assert not calendar.is_session(holiday), f"{holiday} should not be a session"


def test_day_before_and_after_thanksgiving_2024_are_sessions(calendar):
    # Sanity check that only the holiday itself is excluded, not the
    # whole week around it.
    assert calendar.is_session(date(2024, 11, 27))
    assert calendar.is_session(date(2024, 11, 29))  # day-after is a half day, still a session


def test_juneteenth_before_2022_was_not_yet_an_nyse_holiday(calendar):
    # Juneteenth became an NYSE holiday starting 2022 -- 2021-06-18 (the
    # nearest weekday) must NOT be excluded by this calendar.
    assert calendar.is_session(date(2021, 6, 18))


# -- observed-date shifting -----------------------------------------------


def test_holiday_falling_on_saturday_is_observed_the_preceding_friday(calendar):
    # Independence Day 2026 falls on a Saturday -> observed Friday, 2026-07-03.
    assert not calendar.is_session(date(2026, 7, 3))
    assert not calendar.is_session(date(2026, 7, 4))  # the actual Saturday isn't a session anyway (weekend)


def test_holiday_falling_on_sunday_is_observed_the_following_monday(calendar):
    # New Year's Day 2023 falls on a Sunday -> observed Monday, 2023-01-02.
    assert not calendar.is_session(date(2023, 1, 2))
    assert calendar.is_session(date(2023, 1, 3))


# -- session_open / session_close -----------------------------------------


def test_session_open_is_0930_eastern_converted_to_utc(calendar):
    # 2024-01-02 is EST (UTC-5): 09:30 ET == 14:30 UTC.
    opened = calendar.session_open(date(2024, 1, 2))
    assert opened == pd.Timestamp("2024-01-02 14:30:00", tz="UTC")


def test_session_close_is_1600_eastern_converted_to_utc(calendar):
    closed = calendar.session_close(date(2024, 1, 2))
    assert closed == pd.Timestamp("2024-01-02 21:00:00", tz="UTC")


def test_session_open_close_reflect_daylight_saving(calendar):
    # 2024-07-01 is EDT (UTC-4): 09:30 ET == 13:30 UTC.
    opened = calendar.session_open(date(2024, 7, 1))
    assert opened == pd.Timestamp("2024-07-01 13:30:00", tz="UTC")


def test_session_open_raises_for_a_non_trading_day(calendar):
    with pytest.raises(ValueError):
        calendar.session_open(date(2024, 1, 1))  # New Year's Day


def test_session_close_raises_for_a_weekend(calendar):
    with pytest.raises(ValueError):
        calendar.session_close(date(2024, 1, 6))  # Saturday


# -- sessions_between -------------------------------------------------------


def test_sessions_between_excludes_weekends_and_holidays(calendar):
    sessions = calendar.sessions_between(date(2024, 12, 23), date(2024, 12, 27))
    # 12/23 Mon, 12/24 Tue (session), 12/25 Wed (Christmas, excluded),
    # 12/26 Thu, 12/27 Fri -- weekend not in range.
    assert date(2024, 12, 25) not in sessions
    assert sessions == [date(2024, 12, 23), date(2024, 12, 24), date(2024, 12, 26), date(2024, 12, 27)]


def test_sessions_between_rejects_start_after_end(calendar):
    with pytest.raises(ValueError):
        calendar.sessions_between(date(2024, 6, 1), date(2024, 1, 1))


def test_year_outside_supported_range_raises(calendar):
    with pytest.raises(ValueError):
        calendar.is_session(date(2050, 1, 3))


# -- session_date (base class helper) ---------------------------------------


def test_session_date_converts_to_local_exchange_date(calendar):
    # 2024-01-02 14:35 UTC == 2024-01-02 09:35 America/New_York.
    ts = pd.Timestamp("2024-01-02 14:35:00", tz="UTC")
    assert calendar.session_date(ts) == date(2024, 1, 2)


def test_session_date_rejects_a_naive_timestamp(calendar):
    with pytest.raises(ValueError):
        calendar.session_date(pd.Timestamp("2024-01-02 09:35:00"))


def test_trading_calendar_is_abstract():
    with pytest.raises(TypeError):
        TradingCalendar()  # type: ignore[abstract]
