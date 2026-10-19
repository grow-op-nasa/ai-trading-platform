"""Yahoo Finance implementation of DataProvider.

This is one interchangeable implementation of the DataProvider interface.
If we later move to Polygon or Interactive Brokers, we write a new class
here (or in a sibling module) that implements the same `fetch_candles`
signature -- nothing else in the codebase should need to change.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import yfinance as yf
from loguru import logger

from src.data.base import REQUIRED_COLUMNS, DataProvider, Interval
from src.data.exceptions import DataProviderError, NoDataError


def vendor_request_range(start: date, end: date) -> tuple[date, date]:
    """Translate the platform's INCLUSIVE `[start, end]` calendar-date range into yfinance's `[start, end)` one.

    `DataProvider.fetch_candles` promises that `end` is the last day to include (`MarketDataService` slices inclusively
    and the scanner/paper runtime pass today's date expecting today's session bar to be eligible). yfinance treats
    `end` as EXCLUSIVE, so passing it through unchanged silently dropped the bar for `end` itself: every weekday scan
    after the close saw the previous session as the latest bar and was classified DATA_STALE (Sprint 22 follow-up).
    """
    return start, end + timedelta(days=1)


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

        vendor_start, vendor_end = vendor_request_range(start, end)
        try:
            ticker = yf.Ticker(symbol)
            raw = ticker.history(
                start=vendor_start,
                end=vendor_end,
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

    # yfinance returns intraday bars timezone-aware in the exchange's
    # own local timezone, and daily+ bars timezone-naive (a date has no
    # meaningful time-of-day/timezone). Both conventions are left as-is
    # here -- `MarketDataService` canonicalizes to UTC uniformly
    # (`src.data.canonical.canonicalize_candles`, `DECISIONS.md`
    # ADR-0041) rather than each provider converting its own way.

    df = df[~df.index.duplicated(keep="last")]
    df = df.sort_index()

    return df
