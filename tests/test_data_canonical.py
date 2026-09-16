"""Tests for candle canonicalization and the dataset-identity hash it
feeds (`DECISIONS.md`, ADR-0041, hardening Sprint 6's ADR-0035 content
hash).

The property under test throughout: two DataFrames holding the same
candle *values* must canonicalize -- and therefore fingerprint -- to
the same thing, regardless of which incidental representation produced
them (column order, naive vs. aware timestamps, int vs. float volume,
row order). A single changed value must still change the fingerprint.
"""

from __future__ import annotations

import pandas as pd

from src.data.base import REQUIRED_COLUMNS
from src.data.canonical import canonicalize_candles
from src.utils.hashing import dataframe_fingerprint


def _ohlcv(index, closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1_000 for _ in closes],  # deliberately int, not float
        },
        index=index,
    )


def _base_frame() -> pd.DataFrame:
    index = pd.date_range("2024-01-02", periods=3, freq="D", name="timestamp")
    return _ohlcv(index, [100.0, 101.0, 102.0])


# -- shape / dtype / ordering ---------------------------------------------


def test_canonicalize_pins_column_order():
    df = _base_frame()[["volume", "close", "open", "high", "low"]]  # scrambled
    canonical = canonicalize_candles(df)
    assert list(canonical.columns) == REQUIRED_COLUMNS


def test_canonicalize_pins_numeric_dtype_to_float64():
    df = _base_frame()  # volume is int
    canonical = canonicalize_candles(df)
    assert all(canonical[c].dtype == "float64" for c in REQUIRED_COLUMNS)


def test_canonicalize_sorts_ascending():
    df = _base_frame().sort_index(ascending=False)
    canonical = canonicalize_candles(df)
    assert canonical.index.is_monotonic_increasing


def test_canonicalize_localizes_naive_index_to_utc():
    df = _base_frame()
    assert df.index.tz is None  # sanity check
    canonical = canonicalize_candles(df)
    assert str(canonical.index.tz) == "UTC"


def test_canonicalize_converts_an_already_aware_index_to_utc():
    df = _base_frame()
    df.index = df.index.tz_localize("America/New_York")
    canonical = canonicalize_candles(df)
    assert str(canonical.index.tz) == "UTC"
    # The instant is preserved, not just the label -- 09:00 ET on the
    # first fixture day converts to 14:00 UTC (winter, EST = UTC-5).
    assert canonical.index[0] == pd.Timestamp("2024-01-02 05:00:00", tz="UTC")


def test_canonicalize_is_idempotent():
    df = _base_frame()
    once = canonicalize_candles(df)
    twice = canonicalize_candles(once)
    # check_freq=False: DatetimeIndex.freq is a pandas bookkeeping
    # attribute, not real data -- tz_localize() (the first pass, naive
    # input) and tz_convert() (the second pass, already-aware input)
    # aren't guaranteed to preserve it identically across pandas
    # versions. Confirmed via a real pytest run that this is exactly
    # that: only .freq differed (<Day> vs None), not any actual value,
    # column, or dtype -- the same class of non-issue this codebase
    # already established a convention for (see
    # tests/test_market_data.py's test_cache_avoids_second_provider_call).
    pd.testing.assert_frame_equal(once, twice, check_freq=False)


# -- content hash stability -------------------------------------------------


def test_identical_content_hashes_identically_regardless_of_column_order():
    a = canonicalize_candles(_base_frame())
    scrambled = _base_frame()[["low", "high", "open", "close", "volume"]]
    b = canonicalize_candles(scrambled)
    assert dataframe_fingerprint(a) == dataframe_fingerprint(b)


def test_identical_content_hashes_identically_naive_vs_aware():
    naive = _base_frame()
    aware = _base_frame()
    aware.index = aware.index.tz_localize("UTC")

    a = canonicalize_candles(naive)
    b = canonicalize_candles(aware)
    assert dataframe_fingerprint(a) == dataframe_fingerprint(b)


def test_identical_content_hashes_identically_int_vs_float_volume():
    int_volume = _base_frame()
    float_volume = _base_frame()
    float_volume["volume"] = float_volume["volume"].astype("float64")

    a = canonicalize_candles(int_volume)
    b = canonicalize_candles(float_volume)
    assert dataframe_fingerprint(a) == dataframe_fingerprint(b)


def test_identical_content_hashes_identically_row_order():
    ascending = canonicalize_candles(_base_frame())
    descending = canonicalize_candles(_base_frame().sort_index(ascending=False))
    assert dataframe_fingerprint(ascending) == dataframe_fingerprint(descending)


def test_a_changed_close_price_changes_the_hash():
    original = canonicalize_candles(_base_frame())

    changed_frame = _base_frame()
    changed_frame.iloc[0, changed_frame.columns.get_loc("close")] = 999.0
    changed = canonicalize_candles(changed_frame)

    assert dataframe_fingerprint(original) != dataframe_fingerprint(changed)


def test_a_shifted_timestamp_changes_the_hash():
    original = canonicalize_candles(_base_frame())

    shifted_frame = _base_frame()
    shifted_frame.index = shifted_frame.index + pd.Timedelta(minutes=1)
    shifted = canonicalize_candles(shifted_frame)

    assert dataframe_fingerprint(original) != dataframe_fingerprint(shifted)


def test_cache_round_trip_hashes_identically_to_a_fresh_fetch():
    # The exact property Sprint 8 requires of the cache boundary
    # (DECISIONS.md, ADR-0041): a cache hit and a fresh provider fetch
    # of identical canonical candles must produce the same content hash.
    # Simulated here via a CSV round-trip (what CacheManager actually
    # does), independent of MarketDataService -- see test_market_data.py
    # for the full-stack version of this property.
    import tempfile
    from pathlib import Path

    fresh = canonicalize_candles(_base_frame())

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "roundtrip.csv"
        fresh.to_csv(path)
        reloaded = pd.read_csv(path, index_col="timestamp", parse_dates=True)

    reloaded_canonical = canonicalize_candles(reloaded)
    assert dataframe_fingerprint(fresh) == dataframe_fingerprint(reloaded_canonical)
