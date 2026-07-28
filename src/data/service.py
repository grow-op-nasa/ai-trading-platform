"""The Market Data Service.

Its only job: "give me candles for a symbol."

    from src.data import MarketDataService

    service = MarketDataService()
    spy = service.get_candles("SPY")

Every future component -- backtesting, live trading, dashboards, AI
research -- depends on this service rather than on any specific data
vendor. To move from Yahoo Finance to Polygon or Interactive Brokers,
write a new `DataProvider` implementation and pass it in; nothing else
in the system changes.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from dateutil.relativedelta import relativedelta

from src.config.settings import DEFAULT_INTERVAL, DEFAULT_PERIOD
from src.data.base import DataProvider, Interval
from src.data.yfinance_provider import YFinanceProvider
from src.utils.cache import CacheManager

DEFAULT_LOOKBACK_DAYS = 365
DEFAULT_CACHE_DIR = Path("data/cache")

# Matches yfinance-style period shorthand: "5d", "3wk", "6mo", "2y".
_PERIOD_PATTERN = re.compile(r"^(\d+)(d|wk|mo|y)$")


def period_to_start(period: str, end: date) -> date:
    """Convert a yfinance-style period string into a start date.

    Args:
        period: e.g. "5d", "3wk", "6mo", "2y".
        end: the date the period is measured back from.

    Raises:
        ValueError: `period` doesn't match the expected pattern.
    """
    match = _PERIOD_PATTERN.match(period)
    if not match:
        raise ValueError(
            f"Invalid period '{period}'; expected a number followed by "
            "d, wk, mo, or y (e.g. '5d', '3wk', '6mo', '2y')"
        )

    amount, unit = int(match.group(1)), match.group(2)
    if unit == "d":
        return end - timedelta(days=amount)
    if unit == "wk":
        return end - timedelta(weeks=amount)
    if unit == "mo":
        return end - relativedelta(months=amount)
    return end - relativedelta(years=amount)  # unit == "y"


class MarketDataService:
    """The single entry point for historical candle data.

    Fetching and caching are deliberately separate responsibilities:
    this class decides *when* to use the cache and *what* to fetch when
    it doesn't have data; `CacheManager` (`src/utils/cache.py`) only
    knows how to read/write a DataFrame under a string key and has no
    idea what a "symbol" or "interval" is. See `DECISIONS.md`, ADR-0008.

    Args:
        provider: the DataProvider to use. Defaults to Yahoo Finance.
        cache_dir: directory for on-disk caching. Defaults to data/cache.
            Ignored if `cache` is provided.
        use_cache: whether to read/write the cache by default. Can be
            overridden per-call via `get_candles(..., use_cache=...)`.
        cache: an explicit CacheManager to use instead of constructing
            one from `cache_dir`. Mainly useful for tests or for sharing
            one CacheManager instance across services.
    """

    def __init__(
        self,
        provider: DataProvider | None = None,
        cache_dir: Path | str | None = None,
        use_cache: bool = True,
        cache: CacheManager | None = None,
    ) -> None:
        self._provider = provider or YFinanceProvider()
        self._cache = cache or CacheManager(cache_dir or DEFAULT_CACHE_DIR)
        self._use_cache = use_cache

    def get_candles(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        interval: Interval | str = Interval.DAY_1,
        use_cache: bool | None = None,
    ) -> pd.DataFrame:
        """Return OHLCV candles for `symbol`.

        Args:
            symbol: ticker symbol, e.g. "SPY".
            start: first day to include. Defaults to one year before `end`.
            end: last day to include. Defaults to today.
            interval: candle size. Defaults to daily.
            use_cache: overrides the service-level cache setting for this call.

        Returns:
            A DataFrame indexed by a "timestamp" DatetimeIndex with columns
            open, high, low, close, volume -- sorted ascending, no duplicates.

        Raises:
            ValueError: symbol is empty, or start is after end.
            NoDataError: the request was valid but no data was available.
            DataProviderError: the underlying provider failed.
        """
        symbol = self._validate_symbol(symbol)
        interval = Interval(interval)
        end = end or date.today()
        start = start or (end - timedelta(days=DEFAULT_LOOKBACK_DAYS))

        if start > end:
            raise ValueError(f"start ({start}) must not be after end ({end})")

        should_use_cache = self._use_cache if use_cache is None else use_cache
        cache_key = self._cache_key(symbol, start, end, interval)

        if should_use_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        candles = self._provider.fetch_candles(symbol, start, end, interval)

        if should_use_cache:
            self._cache.set(cache_key, candles)

        return candles

    def get_history(
        self,
        symbol: str,
        period: str = DEFAULT_PERIOD,
        interval: Interval | str = DEFAULT_INTERVAL,
        end: date | None = None,
        use_cache: bool | None = None,
    ) -> pd.DataFrame:
        """Convenience wrapper for get_candles using a period string.

        Same result as `get_candles`, but takes a yfinance-style relative
        period (e.g. "5d", "3wk", "6mo", "2y") instead of an explicit
        `start` date. Defaults to the watchlist-wide settings in
        `src/config/settings.py` (DEFAULT_PERIOD, DEFAULT_INTERVAL) so
        callers that don't care about the specifics get sane values for
        free.

        Args:
            symbol: ticker symbol, e.g. "SPY".
            period: lookback window, e.g. "2y". Defaults to DEFAULT_PERIOD.
            interval: candle size. Defaults to DEFAULT_INTERVAL.
            end: last day to include. Defaults to today.
            use_cache: overrides the service-level cache setting for this call.

        Returns:
            Same shape as `get_candles`.

        Raises:
            ValueError: `period` is malformed, symbol is empty, or the
                resulting date range is invalid.
            NoDataError: the request was valid but no data was available.
            DataProviderError: the underlying provider failed.
        """
        end = end or date.today()
        start = period_to_start(period, end)
        return self.get_candles(
            symbol, start=start, end=end, interval=interval, use_cache=use_cache
        )

    def _validate_symbol(self, symbol: str) -> str:
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol must be a non-empty string")
        return symbol

    def _cache_key(self, symbol: str, start: date, end: date, interval: Interval) -> str:
        return f"{symbol}_{interval.value}_{start.isoformat()}_{end.isoformat()}"
