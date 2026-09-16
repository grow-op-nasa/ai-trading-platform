"""Tests for candle data validation (`DECISIONS.md`, ADR-0041, formally
implementing the long-deferred ADR-0006).

Structured around the three-way distinction the sprint's spec draws:
structural failures (the DataFrame doesn't satisfy the candle contract
at all), financial-sanity failures (it does, but a record is
impossible), and suspicious-but-possible data (never raised, only
reported). Gap detection gets its own section since it's the one check
that needs a `TradingCalendar` to mean anything.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.calendar.nyse import NYSECalendar
from src.data.base import Interval
from src.data.exceptions import FinancialSanityError, StructuralValidationError
from src.data.validation import validate_candles

CALENDAR = NYSECalendar()


def _ohlcv(index: pd.DatetimeIndex, closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1_000.0] * len(closes),
        },
        index=index,
    )


def valid_daily_candles(days: list, closes: list[float] | None = None) -> pd.DataFrame:
    closes = closes or [100.0 + i for i in range(len(days))]
    index = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in days], name="timestamp")
    return _ohlcv(index, closes)


def full_session_minute_candles(day) -> pd.DataFrame:
    """A complete, gap-free grid of 1-minute candles for one NYSE
    regular session -- built the same way validation's own gap
    detection builds its expected grid, so a full grid is provably gap-
    free by construction."""
    index = pd.date_range(
        CALENDAR.session_open(day), CALENDAR.session_close(day), freq="1min", inclusive="left"
    )
    index.name = "timestamp"
    closes = [100.0 + (i % 5) for i in range(len(index))]
    return _ohlcv(index, closes)


# -- structural validation ---------------------------------------------------


def test_valid_daily_candles_pass():
    df = valid_daily_candles([pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")])
    report = validate_candles(df, symbol="SPY", interval=Interval.DAY_1)
    assert report.symbol == "SPY"
    assert report.interval is Interval.DAY_1


def test_empty_dataframe_passes_trivially():
    df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df.index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
    report = validate_candles(df, symbol="SPY", interval=Interval.DAY_1)
    assert report.gaps.count == 0
    assert report.suspicious == ()


def test_empty_symbol_raises_structural_error():
    df = valid_daily_candles([pd.Timestamp("2024-01-02")])
    with pytest.raises(StructuralValidationError):
        validate_candles(df, symbol="", interval=Interval.DAY_1)


def test_missing_required_column_raises_structural_error():
    df = valid_daily_candles([pd.Timestamp("2024-01-02")]).drop(columns=["volume"])
    with pytest.raises(StructuralValidationError):
        validate_candles(df, symbol="SPY", interval=Interval.DAY_1)


def test_non_datetime_index_raises_structural_error():
    df = valid_daily_candles([pd.Timestamp("2024-01-02")])
    df.index = pd.Index(["not-a-date"])
    with pytest.raises(StructuralValidationError):
        validate_candles(df, symbol="SPY", interval=Interval.DAY_1)


def test_timezone_naive_index_raises_structural_error():
    df = valid_daily_candles([pd.Timestamp("2024-01-02")])
    df.index = df.index.tz_localize(None)
    with pytest.raises(StructuralValidationError):
        validate_candles(df, symbol="SPY", interval=Interval.DAY_1)


def test_duplicate_timestamps_raise_structural_error():
    days = [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")]
    df = valid_daily_candles(days)
    with pytest.raises(StructuralValidationError):
        validate_candles(df, symbol="SPY", interval=Interval.DAY_1)


def test_unsorted_index_raises_structural_error():
    days = [pd.Timestamp("2024-01-03"), pd.Timestamp("2024-01-02")]
    df = valid_daily_candles(days)
    with pytest.raises(StructuralValidationError):
        validate_candles(df, symbol="SPY", interval=Interval.DAY_1)


def test_nan_in_required_column_raises_structural_error():
    df = valid_daily_candles([pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")])
    df.loc[df.index[0], "close"] = float("nan")
    with pytest.raises(StructuralValidationError):
        validate_candles(df, symbol="SPY", interval=Interval.DAY_1)


# -- financial sanity validation ----------------------------------------------


def test_high_below_max_open_close_raises_financial_sanity_error():
    df = valid_daily_candles([pd.Timestamp("2024-01-02")])
    df.loc[df.index[0], "high"] = df.loc[df.index[0], "open"] - 10  # below open and close
    with pytest.raises(FinancialSanityError):
        validate_candles(df, symbol="SPY", interval=Interval.DAY_1)


def test_low_above_min_open_close_raises_financial_sanity_error():
    df = valid_daily_candles([pd.Timestamp("2024-01-02")])
    df.loc[df.index[0], "low"] = df.loc[df.index[0], "open"] + 10  # above open and close
    with pytest.raises(FinancialSanityError):
        validate_candles(df, symbol="SPY", interval=Interval.DAY_1)


def test_high_below_low_raises_financial_sanity_error():
    df = valid_daily_candles([pd.Timestamp("2024-01-02")])
    df.loc[df.index[0], "high"] = 1.0
    df.loc[df.index[0], "low"] = 100.0
    with pytest.raises(FinancialSanityError):
        validate_candles(df, symbol="SPY", interval=Interval.DAY_1)


def test_negative_volume_raises_financial_sanity_error():
    df = valid_daily_candles([pd.Timestamp("2024-01-02")])
    df.loc[df.index[0], "volume"] = -1.0
    with pytest.raises(FinancialSanityError):
        validate_candles(df, symbol="SPY", interval=Interval.DAY_1)


def test_zero_volume_is_not_a_financial_sanity_violation():
    # Zero volume is suspicious, not impossible -- see the suspicious
    # section below. Must not raise.
    df = valid_daily_candles([pd.Timestamp("2024-01-02")])
    df.loc[df.index[0], "volume"] = 0.0
    validate_candles(df, symbol="SPY", interval=Interval.DAY_1)  # does not raise


# -- suspicious-but-possible data ----------------------------------------------


def test_zero_volume_is_reported_as_suspicious_not_rejected():
    df = valid_daily_candles([pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")])
    df.loc[df.index[0], "volume"] = 0.0
    report = validate_candles(df, symbol="SPY", interval=Interval.DAY_1)
    assert len(report.suspicious) == 1
    assert "zero volume" in report.suspicious[0]


def test_no_suspicious_entries_for_ordinary_data():
    df = valid_daily_candles([pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")])
    report = validate_candles(df, symbol="SPY", interval=Interval.DAY_1)
    assert report.suspicious == ()
    assert report.is_clean


# -- gap detection --------------------------------------------------------------


def test_no_calendar_means_gap_detection_is_skipped():
    df = full_session_minute_candles(pd.Timestamp("2024-01-02")).drop(
        full_session_minute_candles(pd.Timestamp("2024-01-02")).index[100]
    )
    report = validate_candles(df, symbol="SPY", interval=Interval.MINUTE_1, calendar=None)
    assert report.gaps.count == 0  # not detected -- no calendar was given


def test_full_session_grid_has_no_intraday_gaps():
    df = full_session_minute_candles(pd.Timestamp("2024-01-02"))
    report = validate_candles(df, symbol="SPY", interval=Interval.MINUTE_1, calendar=CALENDAR)
    assert report.gaps.count == 0
    assert report.is_clean


def test_a_missing_intraday_bar_is_detected_as_a_gap():
    full = full_session_minute_candles(pd.Timestamp("2024-01-02"))
    missing_ts = full.index[100]  # some bar well inside the session
    df = full.drop(missing_ts)
    report = validate_candles(df, symbol="SPY", interval=Interval.MINUTE_1, calendar=CALENDAR)
    assert report.gaps.has_gaps
    assert missing_ts in report.gaps.missing_timestamps


def test_overnight_gap_between_two_full_sessions_is_not_flagged():
    # Two consecutive, individually complete trading days -- the large
    # jump from 16:00 one day to 09:30 the next is a legitimate
    # overnight gap, not a missing bar (Sprint 8 spec, section 5).
    day1 = full_session_minute_candles(pd.Timestamp("2024-01-02"))
    day2 = full_session_minute_candles(pd.Timestamp("2024-01-03"))
    df = pd.concat([day1, day2])
    report = validate_candles(df, symbol="SPY", interval=Interval.MINUTE_1, calendar=CALENDAR)
    assert report.gaps.count == 0


def test_weekend_gap_between_friday_and_monday_is_not_flagged():
    friday = full_session_minute_candles(pd.Timestamp("2024-01-05"))
    monday = full_session_minute_candles(pd.Timestamp("2024-01-08"))
    df = pd.concat([friday, monday])
    report = validate_candles(df, symbol="SPY", interval=Interval.MINUTE_1, calendar=CALENDAR)
    assert report.gaps.count == 0


def test_a_missing_daily_bar_on_a_trading_day_is_detected():
    # 2024-01-02, 01-03, 01-04 are all NYSE trading days -- skipping
    # 01-03 entirely is a real gap, distinct from any weekend/holiday.
    df = valid_daily_candles([pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-04")])
    report = validate_candles(df, symbol="SPY", interval=Interval.DAY_1, calendar=CALENDAR)
    assert report.gaps.has_gaps
    assert pd.Timestamp("2024-01-03", tz="UTC") in report.gaps.missing_timestamps


def test_daily_bars_across_a_holiday_have_no_gap():
    # 2024-01-01 (New Year's Day) is not an NYSE session -- going
    # straight from 2023-12-29 to 2024-01-02 is not a gap.
    df = valid_daily_candles([pd.Timestamp("2023-12-29"), pd.Timestamp("2024-01-02")])
    report = validate_candles(df, symbol="SPY", interval=Interval.DAY_1, calendar=CALENDAR)
    assert report.gaps.count == 0


def test_weekly_and_monthly_intervals_are_not_gap_checked():
    # Deliberately out of scope this round (module docstring / Sprint 8
    # spec, section 9's "don't overbuild") -- must not raise or flag.
    df = valid_daily_candles([pd.Timestamp("2024-01-02"), pd.Timestamp("2024-02-02")])
    report = validate_candles(df, symbol="SPY", interval=Interval.WEEK_1, calendar=CALENDAR)
    assert report.gaps.count == 0
