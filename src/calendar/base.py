"""The TradingCalendar interface -- src/calendar/base.py.

This is the seam for market-session awareness (`DECISIONS.md`, ADR-0041
-- Sprint 8, "Market Data Integrity & Session Awareness"), analogous to
`src.data.base.DataProvider` (ADR-0002): every current and future
market calendar (NYSE today; NASDAQ, a future FX session model, or
another exchange tomorrow) implements this one interface. Nothing
outside `src/calendar/` and `src/data/` should hardcode session
assumptions (09:30-16:00 ET, specific holidays) directly -- code should
depend on `TradingCalendar` so that adding a new market never requires
touching the strategy engine, the backtester, or the data-validation
layer (`ROADMAP.md`, Sprint 8 acceptance criteria: "market calendar
abstraction exists").

Scope, deliberately: this is not a complete exchange-calendar platform
(`DECISIONS.md`, ADR-0041's explicit non-goals). It answers exactly
three questions -- is this date a trading day, when does the regular
session open/close, which dates in a range are trading days -- for the
one market this platform actually trades today (US equities via NYSE
hours). Pre-market/after-hours sessions, other asset classes, and a
general-purpose calendar library integration are explicitly out of
scope for this round.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd


class TradingCalendar(ABC):
    """Abstract base class for a market's trading-session calendar.

    All timestamps returned are timezone-aware and expressed in UTC --
    the platform's canonical internal representation (`DECISIONS.md`,
    ADR-0041, resolving the long-deferred ADR-0006). Implementations
    convert from the exchange's own local timezone internally; callers
    never need to know what that local timezone is to use this
    interface correctly.
    """

    @abstractmethod
    def is_session(self, day: date) -> bool:
        """True if `day` is a regular trading day for this market.

        False for weekends and market holidays. Does not distinguish
        "not a session" reasons (weekend vs. holiday) -- callers that
        need that distinction should use `sessions_between` and infer
        it, or extend a concrete calendar with a dedicated method.
        """
        raise NotImplementedError

    @abstractmethod
    def session_open(self, day: date) -> pd.Timestamp:
        """The regular-session open instant for `day`, as a UTC timestamp.

        Raises:
            ValueError: `day` is not a trading day (`is_session` is False).
        """
        raise NotImplementedError

    @abstractmethod
    def session_close(self, day: date) -> pd.Timestamp:
        """The regular-session close instant for `day`, as a UTC timestamp.

        Raises:
            ValueError: `day` is not a trading day (`is_session` is False).
        """
        raise NotImplementedError

    def sessions_between(self, start: date, end: date) -> list[date]:
        """All trading days in `[start, end]`, inclusive, ascending.

        A default implementation in terms of `is_session` -- concrete
        calendars may override this for efficiency, but correctness
        only ever depends on `is_session` being right.

        Raises:
            ValueError: `start` is after `end`.
        """
        if start > end:
            raise ValueError(f"start ({start}) must not be after end ({end})")
        return [
            day
            for day in pd.date_range(start, end, freq="D").date
            if self.is_session(day)
        ]

    def session_date(self, timestamp: pd.Timestamp) -> date:
        """The trading-session date `timestamp` belongs to.

        Distinguishes a candle's precise instant from the trading
        session it belongs to (Sprint 8 spec, section 3): converts
        `timestamp` to this calendar's local exchange timezone first,
        then takes that local date. For the NYSE regular-session-only
        policy this round, an intraday candle's session date is simply
        its local calendar date -- the distinction matters more once
        overnight/extended-hours sessions are modeled (deferred; see
        `DECISIONS.md`, ADR-0041's non-goals), but the seam exists now
        so that future work doesn't have to retrofit it.

        Raises:
            ValueError: `timestamp` is timezone-naive. Every timestamp
                reaching this layer must already be tz-aware
                (`DECISIONS.md`, ADR-0041) -- a naive timestamp here
                would be genuinely ambiguous, not something to guess at.
        """
        if timestamp.tzinfo is None:
            raise ValueError(
                f"session_date() requires a timezone-aware timestamp, got "
                f"naive timestamp {timestamp!r}"
            )
        return timestamp.tz_convert(self.local_timezone).date()

    @property
    @abstractmethod
    def local_timezone(self) -> str:
        """The IANA timezone name this market's sessions are defined in."""
        raise NotImplementedError
