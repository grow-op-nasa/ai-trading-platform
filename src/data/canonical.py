"""Canonical candle representation -- src/data/canonical.py.

Sprint 8 (`DECISIONS.md`, ADR-0041) makes Sprint 6's content hash
(`src.utils.hashing.dataframe_fingerprint`, ADR-0035) actually
trustworthy by giving it a single, explicit choke point to run against.
Before this module existed, whatever DataFrame a caller happened to
pass to `dataframe_fingerprint()` was hashed as-is -- two DataFrames
holding identical candle data could still fingerprint differently if
one had a different column order, a different numeric dtype (an int
volume vs. a float volume after a CSV round-trip), or a differently
localized index. That's not a content identity, it's an accident of
whichever code path produced the DataFrame.

`canonicalize_candles()` is that choke point: `MarketDataService` runs
it on every DataFrame it returns, whether freshly fetched or served
from cache (`DECISIONS.md`, ADR-0041), so "cache hit" and "fresh
provider fetch of the same underlying data" are guaranteed to hash
identically. Idempotent by construction -- canonicalizing already-
canonical data is a no-op -- so it's always safe to call again rather
than needing to track whether a given DataFrame has already been
through it.
"""

from __future__ import annotations

import pandas as pd

from src.data.base import REQUIRED_COLUMNS

# Pinned dtypes for the canonical representation. Volume is stored as
# float64 (not int64): a CSV round-trip through pandas silently
# promotes an int column to float the moment it contains any NaN, so
# pinning float64 up front makes the cache-read path and the
# fresh-fetch path agree on dtype without requiring volume to always be
# whole-number-exact on every provider (some providers report adjusted
# or fractional volume).
_CANONICAL_DTYPES = {column: "float64" for column in REQUIRED_COLUMNS}


def canonicalize_candles(df: pd.DataFrame) -> pd.DataFrame:
    """Return a canonical copy of `df` -- deterministic shape, dtype,
    ordering, and timezone, regardless of what produced it.

    Canonicalization rules (`DECISIONS.md`, ADR-0041):
      - row order: sorted ascending by timestamp
      - column order: pinned to `src.data.base.REQUIRED_COLUMNS`
      - numeric representation: every OHLCV column cast to float64
      - timezone: the index is timezone-aware and expressed in UTC --
        a naive index is assumed to already represent UTC instants and
        is localized as such; an already-aware index is converted

    This function does not validate content (see `src.data.validation`
    for that) and does not repair bad data -- a DataFrame missing a
    required column still raises here, loudly, via the same
    `KeyError` pandas would raise for any other missing-column access.
    Two DataFrames holding the same candle values are guaranteed to
    produce byte-identical output from this function, and therefore an
    identical `dataframe_fingerprint()` (`src.utils.hashing`).
    """
    canonical = df[REQUIRED_COLUMNS].copy()
    canonical.index.name = "timestamp"

    if canonical.index.tz is None:
        canonical.index = canonical.index.tz_localize("UTC")
    else:
        canonical.index = canonical.index.tz_convert("UTC")

    canonical = canonical.astype(_CANONICAL_DTYPES)
    canonical = canonical.sort_index()

    # DatetimeIndex.freq is pandas-internal bookkeeping metadata, not
    # candle content: it is inferred lazily and inconsistently depending
    # on which operations produced the index (a naive index run through
    # tz_localize() vs. an already-aware index run through tz_convert()
    # are not guaranteed, across pandas versions, to agree on whether to
    # keep or clear it -- confirmed via a real pytest run where two
    # canonicalization passes over identical data disagreed on freq
    # alone: <Day> vs. None). Left alone, that makes freq an accidental,
    # non-deterministic component of "canonical" output, which breaks
    # both idempotence (`canonicalize(canonicalize(x)) != canonicalize(x)`
    # under a freq-sensitive comparison) and the cache/fresh-fetch
    # identity guarantee this module exists to provide. Pin it to None
    # explicitly so canonical output can never carry inferred frequency
    # metadata, regardless of what pandas happened to infer along the way.
    if canonical.index.freq is not None:
        canonical.index.freq = None

    return canonical
