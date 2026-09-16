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


class DataValidationError(Exception):
    """Base class for a candle dataset failing validation
    (`src.data.validation`, `DECISIONS.md` ADR-0041, formally
    implementing the long-deferred ADR-0006).

    Raised for data that is rejected outright -- never for data that is
    merely unusual (see `ValidationReport.suspicious` for that
    distinction). Not raised directly; catch one of the two concrete
    subclasses below, or this base class to catch either.
    """


class StructuralValidationError(DataValidationError):
    """The DataFrame itself doesn't satisfy the candle contract:
    missing/duplicate/unordered timestamps, a timezone-naive index,
    missing OHLCV columns, or missing symbol/interval metadata.
    """


class FinancialSanityError(DataValidationError):
    """The DataFrame is structurally fine but contains at least one
    financially impossible record: `high < max(open, close)`,
    `low > min(open, close)`, `high < low`, or negative volume.

    Rejected outright rather than repaired (`DECISIONS.md`, ADR-0041):
    silently "fixing" an impossible candle would hide a real data
    problem behind a plausible-looking number.
    """
