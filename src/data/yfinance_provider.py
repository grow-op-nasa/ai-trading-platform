"""Yahoo Finance implementation of DataProvider.

This is one interchangeable implementation of the DataProvider interface.
If we later move to Polygon or Interactive Brokers, we write a new class
here (or in a sibling module) that implements the same `fetch_candles`
signature -- nothing else in the codebase should need to change.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import yfinance as yf
from loguru import logger

from src.data.base import REQUIRED_COLUMNS, DataProvider, Interval
from src.data.exceptions import DataProviderError, NoDataError


class YFinanceProvider(DataProvider):
    """Fetches candles from Yahoo Finance via the `yfinance` package."""

    def fetch_candles(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: Interval,
    ) -> pd.DataFrame:
        logger.debug(
            "Fetching {} candles for {} from {} to {}",
            interval.value,
            symbol,
            start,
            end,
        )

        try:
            ticker = yf.Ticker(symbol)
            raw = ticker.history(
                start=start,
                end=end,
                interval=interval.value,
                auto_adjust=True,
            )
        except Exception as exc:  # yfinance can raise a range of things
            raise DataProviderError(
                f"yfinance failed to fetch {symbol} ({interval.value}): {exc}"
            ) from exc

        if raw.empty:
            raise NoDataError(
                f"No data returned for {symbol} between {start} and {end} "
                f"at interval {interval.value}"
            )

        return _normalize(raw, symbol)


def _normalize(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Convert a raw yfinance DataFrame into the shape DataProvider promises."""
    df = raw.copy()
    df.columns = [str(c).lower() for c in df.columns]

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise DataProviderError(
            f"yfinance response for {symbol} is missing columns: {missing}"
        )

    df = df[REQUIRED_COLUMNS]
    df.index.name = "timestamp"

    # yfinance sometimes returns a tz-aware index; strip it so every
    # provider yields the same tz-naive timestamps.
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    df = df[~df.index.duplicated(keep="last")]
    df = df.sort_index()

    return df
