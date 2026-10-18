"""Strategy Opportunity Scanner (Sprint 21, ADR-0058): a READ-ONLY multi-symbol evaluation of one registered strategy.

Scanning never places an order, creates an intent, cycle or proposal, or touches a portfolio. Its only writes are its own
watchlist and scan-history records in `data/scanner/scanner.db`.
"""

from src.scanner.models import (  # noqa: F401
    MAX_CONCURRENCY,
    MAX_WATCHLIST_SYMBOLS,
    RiskState,
    ResultStatus,
    ScanRecord,
    ScanResult,
    ScanStatus,
    Watchlist,
)
