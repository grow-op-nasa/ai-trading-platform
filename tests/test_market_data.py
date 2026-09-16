"""Tests for the Market Data Service.

These tests use a fake DataProvider so they run instantly and never hit
the network. Network-dependent tests belong in a separate, explicitly
marked integration suite (not written yet) -- unit tests for a service
that depends on an external vendor should never require that vendor to
be up.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.calendar.nyse import NYSECalendar
from src.data.base import REQUIRED_COLUMNS, DataProvider, Interval
from src.data.exceptions import NoDataError
from src.data.models import CandleDataset, SessionPolicy
from src.data.service import MarketDataService, period_to_start
from src.utils.hashing import dataframe_fingerprint


class FakeProvider(DataProvider):
    """A DataProvider double that records calls and returns canned data."""

    def __init__(self, frame: pd.DataFrame | None = None, raises: Exception | None = None):
        self.calls: list[tuple[str, date, date, Interval]] = []
        self._frame = frame
        self._raises = raises

    def fetch_candles(self, symbol, start, end, interval):
        self.calls.append((symbol, start, end, interval))
        if self._raises is not None:
            raise self._raises
        return self._frame


def make_candles(rows: int = 3, start: str = "2024-01-02") -> pd.DataFrame:
    index = pd.date_range(start, periods=rows, freq="D", name="timestamp")
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(rows)],
            "high": [101.0 + i for i in range(rows)],
            "low": [99.0 + i for i in range(rows)],
            "close": [100.5 + i for i in range(rows)],
            "volume": [1_000_000 + i for i in range(rows)],
        },
        index=index,
    )


def test_get_candles_returns_expected_shape(tmp_path):
    provider = FakeProvider(frame=make_candles())
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    result = service.get_candles("spy", start=date(2024, 1, 2), end=date(2024, 1, 5))

    assert list(result.columns) == REQUIRED_COLUMNS
    assert len(result) == 3
    assert result.index.name == "timestamp"


def test_symbol_is_normalized_to_uppercase(tmp_path):
    provider = FakeProvider(frame=make_candles())
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    service.get_candles("spy", start=date(2024, 1, 2), end=date(2024, 1, 5))

    called_symbol = provider.calls[0][0]
    assert called_symbol == "SPY"


def test_empty_symbol_raises_value_error(tmp_path):
    service = MarketDataService(provider=FakeProvider(), cache_dir=tmp_path)

    with pytest.raises(ValueError):
        service.get_candles("   ")


def test_start_after_end_raises_value_error(tmp_path):
    service = MarketDataService(provider=FakeProvider(), cache_dir=tmp_path)

    with pytest.raises(ValueError):
        service.get_candles("SPY", start=date(2024, 6, 1), end=date(2024, 1, 1))


def test_no_data_error_propagates(tmp_path):
    provider = FakeProvider(raises=NoDataError("nothing here"))
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    with pytest.raises(NoDataError):
        service.get_candles("SPY", start=date(2024, 1, 2), end=date(2024, 1, 5))


def test_cache_avoids_second_provider_call(tmp_path):
    # end=2024-01-04 (not -05): must fall within make_candles()'s actual
    # last date (01-02..01-04, 3 rows), so the second request is a
    # genuine full cache hit -- not the Sprint 8 incremental-fetch path
    # trying (and, against this always-returns-the-same-frame fake,
    # failing) to extend past data the fixture never actually provides
    # (DECISIONS.md, ADR-0041, ADR-0007).
    provider = FakeProvider(frame=make_candles())
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    first = service.get_candles("SPY", start=date(2024, 1, 2), end=date(2024, 1, 4))
    second = service.get_candles("SPY", start=date(2024, 1, 2), end=date(2024, 1, 4))

    assert len(provider.calls) == 1  # second call served from cache
    # check_freq=False: the CSV round-trip drops the DatetimeIndex's `freq`
    # metadata (a pandas bookkeeping attribute, not real data), so comparing
    # it would fail for a reason that has nothing to do with correctness.
    pd.testing.assert_frame_equal(first, second, check_freq=False)


def test_use_cache_false_bypasses_cache(tmp_path):
    provider = FakeProvider(frame=make_candles())
    service = MarketDataService(provider=provider, cache_dir=tmp_path, use_cache=True)

    service.get_candles("SPY", start=date(2024, 1, 2), end=date(2024, 1, 5))
    service.get_candles("SPY", start=date(2024, 1, 2), end=date(2024, 1, 5), use_cache=False)

    assert len(provider.calls) == 2


def test_interval_accepts_string(tmp_path):
    provider = FakeProvider(frame=make_candles())
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    service.get_candles("SPY", start=date(2024, 1, 2), end=date(2024, 1, 5), interval="1d")

    assert provider.calls[0][3] == Interval.DAY_1


# -- get_history() / period_to_start() -------------------------------------


def test_period_to_start_days():
    assert period_to_start("5d", date(2024, 1, 10)) == date(2024, 1, 5)


def test_period_to_start_weeks():
    assert period_to_start("2wk", date(2024, 1, 15)) == date(2024, 1, 1)


def test_period_to_start_months():
    assert period_to_start("3mo", date(2024, 4, 15)) == date(2024, 1, 15)


def test_period_to_start_years():
    assert period_to_start("2y", date(2024, 6, 1)) == date(2022, 6, 1)


def test_period_to_start_rejects_malformed_period():
    with pytest.raises(ValueError):
        period_to_start("two years", date(2024, 1, 1))


def test_get_history_delegates_to_get_candles(tmp_path):
    # start=2024-01-05: the fixture's own dates must actually fall
    # within period="5d" ending 2024-01-10 (i.e. [01-05, 01-10]) --
    # Sprint 8's range slicing (DECISIONS.md, ADR-0041) now returns
    # only the requested window, so a fixture dated entirely outside it
    # would (correctly) yield no rows.
    provider = FakeProvider(frame=make_candles(start="2024-01-05"))
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    result = service.get_history("spy", period="5d", interval="1d", end=date(2024, 1, 10))

    called_symbol, called_start, called_end, called_interval = provider.calls[0]
    assert called_symbol == "SPY"
    assert called_start == date(2024, 1, 5)
    assert called_end == date(2024, 1, 10)
    assert called_interval == Interval.DAY_1
    assert list(result.columns) == REQUIRED_COLUMNS


def test_get_history_uses_config_defaults(tmp_path):
    from src.config.settings import DEFAULT_INTERVAL, DEFAULT_PERIOD

    # DEFAULT_INTERVAL ("5m") is intraday, so SessionPolicy.REGULAR
    # (get_history's own default) would filter out this fixture's
    # midnight-timestamped daily-spaced rows entirely -- this test is
    # about config-default plumbing, not session filtering (which has
    # its own dedicated coverage), so session filtering is explicitly
    # opted out of here.
    provider = FakeProvider(frame=make_candles())
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    service.get_history("SPY", end=date(2024, 6, 1), session=SessionPolicy.ALL)

    expected_start = period_to_start(DEFAULT_PERIOD, date(2024, 6, 1))
    called_symbol, called_start, called_end, called_interval = provider.calls[0]
    assert called_start == expected_start
    assert called_interval == Interval(DEFAULT_INTERVAL)


# -- Sprint 8: tz-aware canonical output (DECISIONS.md, ADR-0041) -----------


def test_get_candles_output_is_timezone_aware_utc(tmp_path):
    provider = FakeProvider(frame=make_candles())
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    result = service.get_candles("SPY", start=date(2024, 1, 2), end=date(2024, 1, 4))

    assert str(result.index.tz) == "UTC"


# -- Sprint 8: get_dataset() (DECISIONS.md, ADR-0041) ------------------------


def test_get_dataset_returns_full_metadata(tmp_path):
    provider = FakeProvider(frame=make_candles())
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    dataset = service.get_dataset("SPY", start=date(2024, 1, 2), end=date(2024, 1, 4))

    assert isinstance(dataset, CandleDataset)
    assert dataset.symbol == "SPY"
    assert dataset.interval is Interval.DAY_1
    assert dataset.provider == "FakeProvider"
    assert dataset.timezone == "UTC"
    assert dataset.session_timezone == "America/New_York"
    assert dataset.session_policy is SessionPolicy.REGULAR
    assert dataset.requested_start == date(2024, 1, 2)
    assert dataset.requested_end == date(2024, 1, 4)
    assert dataset.dataset_start == dataset.candles.index.min()
    assert dataset.dataset_end == dataset.candles.index.max()


def test_get_dataset_content_hash_matches_dataframe_fingerprint(tmp_path):
    provider = FakeProvider(frame=make_candles())
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    dataset = service.get_dataset("SPY", start=date(2024, 1, 2), end=date(2024, 1, 4))

    assert dataset.content_hash == dataframe_fingerprint(dataset.candles)


def test_get_dataset_identity_is_the_portable_subset(tmp_path):
    provider = FakeProvider(frame=make_candles())
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    dataset = service.get_dataset("SPY", start=date(2024, 1, 2), end=date(2024, 1, 4))
    identity = dataset.identity

    assert identity.symbol == dataset.symbol
    assert identity.interval == dataset.interval
    assert identity.content_hash == dataset.content_hash
    assert identity.session_policy == dataset.session_policy


def test_cache_hit_and_fresh_fetch_produce_the_same_content_hash(tmp_path):
    # The exact property Sprint 8 requires of the cache boundary: a
    # cache hit and a fresh provider fetch of identical canonical
    # candles must fingerprint identically.
    frame = make_candles()
    service_a = MarketDataService(provider=FakeProvider(frame=frame), cache_dir=tmp_path)
    first = service_a.get_dataset("SPY", start=date(2024, 1, 2), end=date(2024, 1, 4))

    # A second service sharing the same on-disk cache, backed by a
    # provider that raises if actually called -- a matching hash here
    # can only have come from the cache, not a fresh fetch.
    raising_provider = FakeProvider(raises=AssertionError("provider should not be called"))
    service_b = MarketDataService(provider=raising_provider, cache_dir=tmp_path)
    second = service_b.get_dataset("SPY", start=date(2024, 1, 2), end=date(2024, 1, 4))

    assert first.content_hash == second.content_hash


# -- Sprint 8: incremental fetch (DECISIONS.md, ADR-0007 / ADR-0041) ---------


class RangeAwareProvider(DataProvider):
    """A DataProvider double that actually filters a canned full history
    down to `[start, end]`, unlike `FakeProvider` above (which always
    returns the same canned frame regardless of what's asked). Needed
    for testing incremental fetch, where the exact sub-range requested
    is the property under test.
    """

    def __init__(self, full_history: pd.DataFrame):
        self.calls: list[tuple[str, date, date, Interval]] = []
        self._full_history = full_history

    def fetch_candles(self, symbol, start, end, interval):
        self.calls.append((symbol, start, end, interval))
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end) + pd.Timedelta(days=1)
        window = self._full_history[
            (self._full_history.index >= start_ts) & (self._full_history.index < end_ts)
        ]
        if window.empty:
            raise NoDataError(f"no data for {symbol} between {start} and {end}")
        return window


def test_incremental_fetch_only_requests_the_new_tail(tmp_path):
    history = make_candles(rows=10)  # 2024-01-02 .. 2024-01-11
    provider = RangeAwareProvider(history)
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    first = service.get_candles("SPY", start=date(2024, 1, 2), end=date(2024, 1, 5))
    assert len(provider.calls) == 1
    assert len(first) == 4  # 01-02, 03, 04, 05

    second = service.get_candles("SPY", start=date(2024, 1, 2), end=date(2024, 1, 8))
    assert len(provider.calls) == 2
    # Only the new tail was requested -- not the whole range again.
    assert provider.calls[1][1] == date(2024, 1, 6)
    assert provider.calls[1][2] == date(2024, 1, 8)
    assert len(second) == 7  # 01-02 .. 01-08


def test_incremental_fetch_within_the_cached_span_makes_no_provider_call(tmp_path):
    history = make_candles(rows=10)
    provider = RangeAwareProvider(history)
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    service.get_candles("SPY", start=date(2024, 1, 2), end=date(2024, 1, 8))
    assert len(provider.calls) == 1

    # A narrower request fully inside what's already cached: no new
    # provider call at all.
    service.get_candles("SPY", start=date(2024, 1, 3), end=date(2024, 1, 5))
    assert len(provider.calls) == 1


def test_a_requested_start_before_the_cached_range_falls_back_to_a_full_refetch(tmp_path):
    history = make_candles(rows=10)
    provider = RangeAwareProvider(history)
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    service.get_candles("SPY", start=date(2024, 1, 5), end=date(2024, 1, 8))
    assert len(provider.calls) == 1

    service.get_candles("SPY", start=date(2024, 1, 2), end=date(2024, 1, 8))
    assert len(provider.calls) == 2
    # Full refetch of the entire newly requested range -- not a
    # "missing prefix only" fetch (Sprint 8 spec, section 9: the seam,
    # not a general interval-merge system).
    assert provider.calls[1][1] == date(2024, 1, 2)
    assert provider.calls[1][2] == date(2024, 1, 8)


def test_incremental_fetch_produces_the_same_hash_as_a_complete_fetch(tmp_path):
    # The call-count tests above prove the incremental path fetches only
    # the missing tail. They don't prove the *result* is equivalent to a
    # one-shot fetch of the same range -- that the merge-then-canonicalize
    # step doesn't introduce drift (row order, dtype, index metadata).
    # Hash equality is that proof.
    history = make_candles(rows=10)  # 2024-01-02 .. 2024-01-11

    incremental_service = MarketDataService(
        provider=RangeAwareProvider(history), cache_dir=tmp_path / "incremental"
    )
    incremental_service.get_dataset("SPY", start=date(2024, 1, 2), end=date(2024, 1, 5))
    incremental = incremental_service.get_dataset("SPY", start=date(2024, 1, 2), end=date(2024, 1, 8))

    complete_service = MarketDataService(
        provider=RangeAwareProvider(history), cache_dir=tmp_path / "complete"
    )
    complete = complete_service.get_dataset("SPY", start=date(2024, 1, 2), end=date(2024, 1, 8))

    assert incremental.content_hash == complete.content_hash


# -- Sprint 8: session filtering (DECISIONS.md, ADR-0041) --------------------


def make_intraday_candles_with_extended_hours(day: date) -> pd.DataFrame:
    """One regular-session's worth of 1-minute candles, plus one
    pre-market and one after-hours bar -- tz-aware in America/New_York,
    the same convention real yfinance intraday data uses."""
    calendar = NYSECalendar()
    local_open = calendar.session_open(day).tz_convert("America/New_York")
    local_close = calendar.session_close(day).tz_convert("America/New_York")

    timestamps = [local_open - pd.Timedelta(minutes=30)]  # pre-market
    timestamps += list(pd.date_range(local_open, periods=3, freq="1min"))  # regular session
    timestamps.append(local_close + pd.Timedelta(minutes=30))  # after-hours

    index = pd.DatetimeIndex(timestamps, name="timestamp")
    closes = [100.0 + i for i in range(len(index))]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [1_000.0] * len(closes),
        },
        index=index,
    )


def test_session_regular_filters_out_extended_hours_for_intraday(tmp_path):
    day = date(2024, 1, 2)
    provider = FakeProvider(frame=make_intraday_candles_with_extended_hours(day))
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    result = service.get_candles(
        "SPY", start=day, end=day, interval=Interval.MINUTE_1, session=SessionPolicy.REGULAR
    )

    assert len(result) == 3  # only the 3 regular-session bars survive
    local_times = result.index.tz_convert("America/New_York").time
    open_t, close_t = pd.Timestamp("09:30").time(), pd.Timestamp("16:00").time()
    assert all(open_t <= t < close_t for t in local_times)


def test_session_all_returns_every_candle_unfiltered(tmp_path):
    day = date(2024, 1, 2)
    provider = FakeProvider(frame=make_intraday_candles_with_extended_hours(day))
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    result = service.get_candles(
        "SPY", start=day, end=day, interval=Interval.MINUTE_1, session=SessionPolicy.ALL
    )

    assert len(result) == 5  # pre-market + 3 regular + after-hours, all present


def test_session_policy_is_a_noop_for_daily_interval(tmp_path):
    regular = MarketDataService(
        provider=FakeProvider(frame=make_candles()), cache_dir=tmp_path
    ).get_candles(
        "SPY", start=date(2024, 1, 2), end=date(2024, 1, 4), session=SessionPolicy.REGULAR
    )
    all_ = MarketDataService(
        provider=FakeProvider(frame=make_candles()), cache_dir=tmp_path
    ).get_candles(
        "SPY", start=date(2024, 1, 2), end=date(2024, 1, 4), session=SessionPolicy.ALL
    )
    pd.testing.assert_frame_equal(regular, all_, check_freq=False)
