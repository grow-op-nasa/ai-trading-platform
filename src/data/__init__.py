"""Market data capability.

Public entry point: `MarketDataService`, in `service.py`. Sprint 8
(`DECISIONS.md`, ADR-0041) adds `get_dataset()` alongside the
pre-existing `get_candles()`/`get_history()` -- also exporting the
shapes a caller needs to work with its result (`CandleDataset`,
`SessionPolicy`, `ValidationReport`) and the validation errors it can
raise. Everything else in this package (canonicalization internals,
the provider implementation) is an implementation detail.
"""

from src.data.base import DataProvider, Interval
from src.data.exceptions import (
    DataProviderError,
    DataValidationError,
    FinancialSanityError,
    NoDataError,
    StructuralValidationError,
)
from src.data.models import CandleDataset, DatasetIdentity, SessionPolicy, ValidationReport
from src.data.service import MarketDataService

__all__ = [
    "MarketDataService",
    "DataProvider",
    "Interval",
    "CandleDataset",
    "DatasetIdentity",
    "SessionPolicy",
    "ValidationReport",
    "DataProviderError",
    "NoDataError",
    "DataValidationError",
    "StructuralValidationError",
    "FinancialSanityError",
]
