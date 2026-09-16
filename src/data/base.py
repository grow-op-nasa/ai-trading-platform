"""The DataProvider interface.

This is the seam in the system. Every current and future data source
(Yahoo Finance today; Polygon, Interactive Brokers, or anything else
tomorrow) implements this one method. Nothing outside `src/data/` should
ever import a specific provider directly -- code should depend on
`DataProvider` (or better, on `MarketDataService`, see service.py) so that
swapping providers never requires touching strategies, backtests, or the
dashboard.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from enum import Enum

import pandas as pd


class Interval(str, Enum):
    """Candle intervals supported across providers.

    Not every provider supports every interval (e.g. intraday history is
    often capped to the last ~60 days by free data sources). Providers
    should raise `DataProviderError` for combinations they can't serve
    rather than silently returning something else.
    """

    MINUTE_1 = "1m"
    MINUTE_5 = "5m"
    MINUTE_15 = "15m"
    MINUTE_30 = "30m"
    HOUR_1 = "60m"
    DAY_1 = "1d"
    WEEK_1 = "1wk"
    MONTH_1 = "1mo"


# The contract every candle DataFrame must satisfy, regardless of provider.
REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]


class DataProvider(ABC):
    """Abstract base class for a source of historical candle data."""

    @abstractmethod
    def fetch_candles(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: Interval,
    ) -> pd.DataFrame:
        """Fetch raw candles for `symbol` between `start` and `end`.

        Implementations must return a DataFrame that:
          - is indexed by a `DatetimeIndex` named "timestamp", either
            timezone-naive (treated as already representing UTC -- the
            right convention for a date-only daily/weekly/monthly bar
            with no meaningful time-of-day) or timezone-aware in
            whatever zone the vendor actually reports (e.g. exchange-
            local for intraday bars)
          - has exactly the columns in REQUIRED_COLUMNS (lowercase)
          - is sorted ascending by timestamp
          - contains no duplicate timestamps

        Providers do not need to convert to UTC themselves --
        `MarketDataService` canonicalizes every DataFrame it returns
        (`src.data.canonical.canonicalize_candles`, `DECISIONS.md`
        ADR-0041) to a timezone-aware UTC index regardless of which
        convention a given provider's raw output uses. A provider only
        needs to be honest about which convention its own raw
        timestamps follow.

        Raises:
            NoDataError: the request was valid but no data was available.
            DataProviderError: the request could not be completed.
        """
        raise NotImplementedError
