"""Tests for the generic on-disk CacheManager (src/utils/cache.py).

Deliberately has nothing to do with market data -- this cache is meant
to be reused by any future capability (news, options chains, VIX,
macro data, earnings, forex), so its tests shouldn't reference candles,
symbols, or intervals at all.
"""

from __future__ import annotations

import pandas as pd

from src.utils.cache import CacheManager


def make_frame(rows: int = 2) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=rows, freq="D", name="timestamp")
    return pd.DataFrame({"value": [1.0 + i for i in range(rows)]}, index=index)


def test_get_returns_none_for_missing_key(tmp_path):
    cache = CacheManager(tmp_path)

    assert cache.get("does-not-exist") is None


def test_set_then_get_roundtrips(tmp_path):
    cache = CacheManager(tmp_path)
    frame = make_frame()

    cache.set("some-key", frame)
    result = cache.get("some-key")

    assert result is not None
    pd.testing.assert_frame_equal(result, frame, check_freq=False)


def test_set_creates_cache_dir_if_missing(tmp_path):
    nested = tmp_path / "nested" / "cache" / "dir"
    cache = CacheManager(nested)

    cache.set("k", make_frame())

    assert (nested / "k.csv").exists()


def test_different_keys_do_not_collide(tmp_path):
    cache = CacheManager(tmp_path)
    frame_a = make_frame(rows=2)
    frame_b = make_frame(rows=5)

    cache.set("a", frame_a)
    cache.set("b", frame_b)

    assert len(cache.get("a")) == 2
    assert len(cache.get("b")) == 5


def test_custom_index_col(tmp_path):
    cache = CacheManager(tmp_path, index_col="published_at")
    index = pd.date_range("2024-01-01", periods=3, name="published_at")
    frame = pd.DataFrame({"headline": ["a", "b", "c"]}, index=index)

    cache.set("news-item", frame)
    result = cache.get("news-item")

    assert result.index.name == "published_at"
    assert list(result["headline"]) == ["a", "b", "c"]


def test_set_does_not_raise_on_write_failure(tmp_path, monkeypatch):
    cache = CacheManager(tmp_path)

    def broken_to_csv(*args, **kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(pd.DataFrame, "to_csv", broken_to_csv)

    # Should log a warning internally, not raise.
    cache.set("k", make_frame())
