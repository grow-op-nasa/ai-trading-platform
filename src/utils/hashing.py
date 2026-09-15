"""Generic content hashing -- src/utils/hashing.py.

Shared by the two identity seams established in Sprint 6
(`DECISIONS.md`, ADR-0035): strategy version
(`src/strategies/identity.py`) hashes a strategy class's own source
code, dataset fingerprinting (below) hashes the actual candle values
used in a backtest. Both reduce to "hash these bytes, deterministically,
every time" -- kept here once rather than duplicated, the same reasoning
`CacheManager` lives in `src/utils` instead of inside `src/data`
(`DECISIONS.md`, ADR-0008): this has no idea what a "strategy" or a
"candle" is, which is exactly what lets both capabilities depend on it
without depending on each other.
"""

from __future__ import annotations

import hashlib

import pandas as pd


def sha256_hex(data: bytes) -> str:
    """Hex-encoded SHA-256 digest of `data`."""
    return hashlib.sha256(data).hexdigest()


def dataframe_fingerprint(df: pd.DataFrame) -> str:
    """A deterministic hex digest of `df`'s actual values, index, and
    column names.

    Two DataFrames with equal content (same values, same index, same
    column order) always fingerprint identically, regardless of when or
    how they were produced. A single changed value -- one revised close
    price, one shifted timestamp -- changes the fingerprint.

    This is a **content** identity, not a **descriptive** one: it
    answers "is this the same data", not "does this look like the same
    request". A `(symbol, interval, start, end)` descriptor can stay
    identical while the underlying values silently change underneath it
    (a vendor data revision, a stale cache); this fingerprint is what
    makes that gap detectable instead of assumed away
    (`DECISIONS.md`, ADR-0035).
    """
    row_hashes = pd.util.hash_pandas_object(df, index=True)
    digest = hashlib.sha256(row_hashes.to_numpy().tobytes())
    digest.update(",".join(str(column) for column in df.columns).encode("utf-8"))
    return digest.hexdigest()
