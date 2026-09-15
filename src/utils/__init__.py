"""Shared helpers used across capabilities: caching, formatting, and
content hashing (the basis for the strategy-version and dataset-identity
seams established in Sprint 6, `DECISIONS.md` ADR-0035) -- none of it
owned by any one capability.
"""

from src.utils.cache import CacheManager
from src.utils.formatting import format_timedelta
from src.utils.hashing import dataframe_fingerprint, sha256_hex

__all__ = [
    "CacheManager",
    "format_timedelta",
    "dataframe_fingerprint",
    "sha256_hex",
]
