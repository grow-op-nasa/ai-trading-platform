"""Candle data validation -- src/data/validation.py.

Formally implements `DECISIONS.md`'s long-deferred ADR-0006: a
validation step `MarketDataService` runs on every DataFrame it returns
-- whether freshly fetched or served from cache -- before it reaches
research, backtesting, or a strategy (`DECISIONS.md`, ADR-0041,
Sprint 8's architectural invariant).

Two different kinds of "wrong" are deliberately kept separate (Sprint 8
spec, section 4):

  - **Invalid** data is rejected outright, loudly, via one of the two
    `DataValidationError` subclasses (`src.data.exceptions`) --
    `StructuralValidationError` for a DataFrame that doesn't satisfy
    the candle contract at all, `FinancialSanityError` for one that
    does but contains an impossible record (`high < low`, negative
    volume, etc.). Never silently repaired.
  - **Suspicious but technically possible** data (zero volume, a gap
    in an open session) is never raised -- it's recorded on the
    returned `ValidationReport` so a caller *knows* about it rather
    than assuming completeness. This is deliberately not an anomaly
    detector: the checks here are the small, explicit set from the
    sprint's own spec, not a general statistical outlier model.
"""

from __future__ import annotations

import pandas as pd

from src.calendar.base import TradingCalendar
from src.data.base import Interval
from src.data.exceptions import FinancialSanityError, StructuralValidationError
from src.data.models import GapReport, ValidationReport

REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]

# Pandas frequency strings for the intraday intervals gap detection can
# build an expected timestamp grid for. Daily+ intervals are handled
# separately (against calendar.sessions_between()); weekly/monthly are
# deliberately not gap-checked at all -- see module docstring's
# "don't overbuild" framing and the class docstring below.
_INTRADAY_FREQ = {
    Interval.MINUTE_1: "1min",
    Interval.MINUTE_5: "5min",
    Interval.MINUTE_15: "15min",
    Interval.MINUTE_30: "30min",
    Interval.HOUR_1: "60min",
}

_MAX_SUSPICIOUS_EXAMPLES = 3


def validate_candles(
    df: pd.DataFrame,
    *,
    symbol: str,
    interval: Interval,
    calendar: TradingCalendar | None = None,
) -> ValidationReport:
    """Validate `df` against the candle contract and return a report.

    Args:
        df: candles to validate. Expected to already be canonicalized
            (`src.data.canonical.canonicalize_candles`) -- this function
            checks the contract, it doesn't normalize toward it.
        symbol: the instrument `df` is claimed to represent.
        interval: the candle timeframe `df` is claimed to represent.
        calendar: if provided, enables gap detection (Sprint 8 spec,
            section 5) for `Interval.DAY_1` and the intraday intervals.
            If `None`, gap detection is skipped entirely and
            `ValidationReport.gaps` is always empty -- gap detection
            needs session awareness to distinguish a real gap from a
            legitimate overnight/weekend/holiday one, so it can't run
            without a calendar, but validation of everything else
            (structural + financial sanity) never requires one.

    Returns:
        A `ValidationReport` -- only for data that passed. A failure
        raises instead of returning a report marked "invalid".

    Raises:
        StructuralValidationError: `df` doesn't satisfy the candle
            contract (missing/duplicate/unordered timestamps, a
            timezone-naive index, missing OHLCV columns or values, or
            empty `symbol`).
        FinancialSanityError: `df` contains at least one financially
            impossible record.
    """
    if not symbol:
        raise StructuralValidationError("symbol must be non-empty")

    if df.empty:
        return ValidationReport(symbol=symbol, interval=interval)

    _validate_structure(df)
    _validate_financial_sanity(df)

    gaps = (
        _detect_gaps(df, interval=interval, calendar=calendar)
        if calendar is not None
        else GapReport()
    )
    suspicious = _detect_suspicious(df)

    return ValidationReport(symbol=symbol, interval=interval, gaps=gaps, suspicious=suspicious)


def _validate_structure(df: pd.DataFrame) -> None:
    missing_columns = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_columns:
        raise StructuralValidationError(f"missing required columns: {missing_columns}")

    if not isinstance(df.index, pd.DatetimeIndex):
        raise StructuralValidationError(
            f"index must be a DatetimeIndex, got {type(df.index).__name__}"
        )

    if df.index.tz is None:
        raise StructuralValidationError(
            "index must be timezone-aware (DECISIONS.md, ADR-0041) -- got a "
            "naive DatetimeIndex"
        )

    if df.index.has_duplicates:
        duplicate_count = int(df.index.duplicated().sum())
        raise StructuralValidationError(f"{duplicate_count} duplicate timestamp(s) in index")

    if not df.index.is_monotonic_increasing:
        raise StructuralValidationError("index is not sorted ascending")

    ohlcv = df[REQUIRED_COLUMNS]
    if ohlcv.isna().any().any():
        bad_columns = ohlcv.columns[ohlcv.isna().any()].tolist()
        raise StructuralValidationError(f"NaN values found in columns: {bad_columns}")


def _validate_financial_sanity(df: pd.DataFrame) -> None:
    open_, high, low, close, volume = (df[c] for c in REQUIRED_COLUMNS)

    violations = []
    if (high < pd.concat([open_, close], axis=1).max(axis=1)).any():
        violations.append("high < max(open, close)")
    if (low > pd.concat([open_, close], axis=1).min(axis=1)).any():
        violations.append("low > min(open, close)")
    if (high < low).any():
        violations.append("high < low")
    if (volume < 0).any():
        violations.append("negative volume")

    if violations:
        raise FinancialSanityError(
            f"impossible OHLCV record(s) found: {', '.join(violations)}"
        )


def _detect_suspicious(df: pd.DataFrame) -> tuple[str, ...]:
    suspicious: list[str] = []

    zero_volume = df.index[df["volume"] == 0]
    if len(zero_volume) > 0:
        examples = ", ".join(str(ts) for ts in zero_volume[:_MAX_SUSPICIOUS_EXAMPLES])
        suspicious.append(f"{len(zero_volume)} candle(s) with zero volume (e.g. {examples})")

    return tuple(suspicious)


def _detect_gaps(
    df: pd.DataFrame, *, interval: Interval, calendar: TradingCalendar
) -> GapReport:
    if interval == Interval.DAY_1:
        # A daily bar's timestamp is a calendar-date marker, not a real
        # local-exchange instant (`src.data.canonical`'s documented
        # convention: a naive daily timestamp is treated as UTC
        # midnight of that date). Converting it to exchange-local time
        # before taking `.date()` would shift it to the *previous*
        # calendar date for any exchange behind UTC -- so daily gap
        # detection compares raw UTC dates throughout, matching
        # `_detect_daily_gaps`'s own `present_dates` extraction.
        start_date = df.index[0].date()
        end_date = df.index[-1].date()
        sessions = calendar.sessions_between(start_date, end_date)
        return _detect_daily_gaps(df, sessions) if sessions else GapReport()

    freq = _INTRADAY_FREQ.get(interval)
    if freq is None:
        # Weekly/monthly bars: gap semantics aren't well-defined without
        # additional calendar work (how many weekly bars *should* exist
        # in a range with a holiday-shortened week?) -- deliberately not
        # built this round rather than guessed at.
        return GapReport()

    # Intraday timestamps are real instants, so the exchange-local date
    # they fall on genuinely can differ from their UTC date near
    # midnight -- converting before taking `.date()` here is correct.
    start_date = df.index[0].tz_convert(calendar.local_timezone).date()
    end_date = df.index[-1].tz_convert(calendar.local_timezone).date()
    sessions = calendar.sessions_between(start_date, end_date)
    return _detect_intraday_gaps(df, sessions, freq, calendar) if sessions else GapReport()


def _detect_daily_gaps(df: pd.DataFrame, sessions: list) -> GapReport:
    present_dates = {ts.date() for ts in df.index}
    missing = [
        pd.Timestamp(session, tz=df.index.tz)
        for session in sessions
        if session not in present_dates
    ]
    return GapReport(missing_timestamps=tuple(missing))


def _detect_intraday_gaps(
    df: pd.DataFrame, sessions: list, freq: str, calendar: TradingCalendar
) -> GapReport:
    present = set(df.index)
    missing: list[pd.Timestamp] = []

    for session in sessions:
        session_open = calendar.session_open(session)
        session_close = calendar.session_close(session)
        expected = pd.date_range(session_open, session_close, freq=freq, inclusive="left")
        missing.extend(ts for ts in expected if ts not in present)

    return GapReport(missing_timestamps=tuple(missing))
