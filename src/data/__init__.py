"""Market data capability.

Public entry point: `MarketDataService`, in `service.py`.
Everything else in this package is an implementation detail.
"""

from src.data.service import MarketDataService

__all__ = ["MarketDataService"]
