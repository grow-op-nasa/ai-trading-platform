"""Exceptions for the data capability."""

from __future__ import annotations


class DataProviderError(Exception):
    """Raised when a DataProvider fails to fetch or parse market data."""


class NoDataError(DataProviderError):
    """Raised when a provider returns no rows for a valid request.

    This is distinct from a connection/parsing failure: the request was
    well-formed and reached the provider, but there was nothing to return
    (e.g. a delisted symbol, or a date range with no trading days).
    """
