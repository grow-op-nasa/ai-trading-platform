"""Tests for generic content hashing (src/utils/hashing.py).

The basis for both identity seams established in Sprint 6
(`DECISIONS.md`, ADR-0035): strategy version and dataset fingerprint.
"""

from __future__ import annotations

import pandas as pd

from src.utils.hashing import dataframe_fingerprint, sha256_hex


def make_candles(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D", name="timestamp")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        index=dates,
    )


def test_sha256_hex_is_deterministic():
    assert sha256_hex(b"hello") == sha256_hex(b"hello")


def test_sha256_hex_differs_for_different_input():
    assert sha256_hex(b"hello") != sha256_hex(b"goodbye")


def test_dataframe_fingerprint_is_deterministic_across_calls():
    candles = make_candles([100, 101, 102])
    assert dataframe_fingerprint(candles) == dataframe_fingerprint(candles.copy())


def test_dataframe_fingerprint_changes_with_a_single_revised_value():
    original = make_candles([100, 101, 102])
    revised = original.copy()
    revised.loc[revised.index[1], "close"] = 999.0

    assert dataframe_fingerprint(original) != dataframe_fingerprint(revised)


def test_dataframe_fingerprint_is_stable_regardless_of_construction_path():
    # Two DataFrames built differently but with identical resulting
    # content fingerprint identically -- content identity, not object
    # identity or construction-path identity.
    a = make_candles([100, 101, 102])
    b = make_candles([100, 101, 102]).astype(a.dtypes.to_dict())

    assert dataframe_fingerprint(a) == dataframe_fingerprint(b)


def test_dataframe_fingerprint_changes_with_different_row_count():
    shorter = make_candles([100, 101])
    longer = make_candles([100, 101, 102])

    assert dataframe_fingerprint(shorter) != dataframe_fingerprint(longer)
