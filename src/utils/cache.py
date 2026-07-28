"""Generic on-disk cache for pandas DataFrames, keyed by a plain string.

Not specific to market data. `MarketDataService` uses this today for
candle caching, but the point of pulling it out of `src/data/` and into
`src/utils/` is reuse: every future capability that fetches from an
external source and wants to avoid re-fetching identical requests --
news, options chains, VIX, macro data, earnings, forex -- should use
this instead of rolling its own file I/O. See `DECISIONS.md`, ADR-0008.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from loguru import logger


class CacheManager:
    """Reads and writes DataFrames to disk as CSV, keyed by a string.

    Args:
        cache_dir: directory to store cache files in. Created lazily on
            first write, not at construction time.
        index_col: name of the column to use as the index when reading
            a cached file back. Defaults to "timestamp" since every
            current use case is time-series data; pass something else
            for a future use case where that doesn't hold.
    """

    def __init__(self, cache_dir: Path | str, index_col: str = "timestamp") -> None:
        self._cache_dir = Path(cache_dir)
        self._index_col = index_col

    def get(self, key: str) -> pd.DataFrame | None:
        """Return the cached DataFrame for `key`, or None if not cached."""
        path = self._path_for(key)
        if not path.exists():
            return None
        logger.debug("Cache hit: {}", path)
        return pd.read_csv(path, index_col=self._index_col, parse_dates=True)

    def set(self, key: str, data: pd.DataFrame) -> None:
        """Persist `data` under `key`.

        A failure to write is logged, not raised: caching is a
        performance optimization, and a caller that already
        successfully fetched its data shouldn't have the request fail
        just because the cache write failed (e.g. disk full,
        permissions).
        """
        path = self._path_for(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data.to_csv(path)
        except OSError as exc:
            logger.warning("Could not write cache file {}: {}", path, exc)

    def _path_for(self, key: str) -> Path:
        return self._cache_dir / f"{key}.csv"
