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

from src.data.base import REQUIRED_COLUMNS, DataProvider, Interval
from src.data.exceptions import NoDataError
from src.data.service import MarketDataService, period_to_start


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


def make_candles(rows: int = 3) -> pd.DataFrame:
    index = pd.date_range("2024-01-02", periods=rows, freq="D", name="timestamp")
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
    provider = FakeProvider(frame=make_candles())
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    first = service.get_candles("SPY", start=date(2024, 1, 2), end=date(2024, 1, 5))
    second = service.get_candles("SPY", start=date(2024, 1, 2), end=date(2024, 1, 5))

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
    provider = FakeProvider(frame=make_candles())
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

    provider = FakeProvider(frame=make_candles())
    service = MarketDataService(provider=provider, cache_dir=tmp_path)

    service.get_history("SPY", end=date(2024, 6, 1))

    expected_start = period_to_start(DEFAULT_PERIOD, date(2024, 6, 1))
    called_symbol, called_start, called_end, called_interval = provider.calls[0]
    assert called_start == expected_start
    assert called_interval == Interval(DEFAULT_INTERVAL)
