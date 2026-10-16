"""Closed-bar and session semantics for daily decisions (Sprint 18, ADR-0053).

Daily bars are stamped at UTC midnight of their trading date (canonical form, ADR-0041). A bar is COMPLETE only
once the exchange session it belongs to has closed; a provider may return today's in-progress bar, which must never
reach a strategy. The latest expected completed session is derived from the same `TradingCalendar` the monitoring
freshness check uses.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from src.calendar.base import TradingCalendar

DAILY_CLOSE_GRACE_MINUTES = 60   # same default as the monitoring freshness check


def bar_session(ts: pd.Timestamp) -> date:
    return pd.Timestamp(ts).tz_convert("UTC").date()


def is_complete(ts: pd.Timestamp, cal: TradingCalendar, now: pd.Timestamp) -> bool:
    day = bar_session(ts)
    return cal.is_session(day) and now >= cal.session_close(day)


def split_complete(candles: pd.DataFrame, cal: TradingCalendar, now: datetime):
    """(complete_candles, dropped_incomplete_timestamps)."""
    now_ts = pd.Timestamp(now).tz_convert("UTC")
    mask = [is_complete(ts, cal, now_ts) for ts in candles.index]
    complete = candles[mask]
    dropped = [ts for ts, ok in zip(candles.index, mask) if not ok]
    return complete, dropped


def expected_completed_session(cal: TradingCalendar, now: datetime) -> date:
    """The most recent session that has closed as of `now`."""
    now_ts = pd.Timestamp(now).tz_convert("UTC")
    day = now_ts.date()
    for _ in range(15):
        if cal.is_session(day) and now_ts >= cal.session_close(day):
            return day
        day -= timedelta(days=1)
    raise ValueError("no completed trading session found within 15 days")


def past_grace(cal: TradingCalendar, session: date, now: datetime) -> bool:
    return pd.Timestamp(now).tz_convert("UTC") >= cal.session_close(session) + timedelta(
        minutes=DAILY_CLOSE_GRACE_MINUTES)
