"""The Market Data Service.

Its only job: "give me trustworthy candles for a symbol."

    from src.data import MarketDataService
    service = MarketDataService()
    spy = service.get_candles("SPY")

Every future component -- backtesting, live trading, dashboards, AI
research -- depends on this service rather than on any specific data
vendor, and (as of Sprint 8, `DECISIONS.md` ADR-0041) rather than on
raw, unvalidated provider output. `get_candles()`/`get_history()`
return a plain `pd.DataFrame`, same as before Sprint 8, but that
DataFrame is now always canonicalized (`src.data.canonical`) and
validated (`src.data.validation`) before it's returned -- whether it
was just fetched or served from cache. `get_dataset()` is new: it
returns the full `CandleDataset` (`src.data.models`) -- candles plus
symbol/timeframe/provider/session/identity metadata -- for callers
that want the richer picture (research, `ExperimentSpec`, a
dataset-aware backtest).

To move from Yahoo Finance to Polygon or Interactive Brokers, write a
new `DataProvider` implementation and pass it in; nothing else in the
system changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from dateutil.relativedelta import relativedelta

from src.calendar import NYSECalendar
from src.calendar.base import TradingCalendar
from src.config.settings import DEFAULT_INTERVAL, DEFAULT_PERIOD
from src.data.base import DataProvider, Interval
from src.data.canonical import canonicalize_candles
from src.data.exceptions import NoDataError
from src.data.models import CandleDataset, SessionPolicy
from src.data.validation import validate_candles
from src.data.yfinance_provider import YFinanceProvider
from src.utils.cache import CacheManager
from src.utils.hashing import dataframe_fingerprint

DEFAULT_LOOKBACK_DAYS = 365
DEFAULT_CACHE_DIR = Path("data/cache")

# Regular-session local hours, applied only to intraday intervals --
# daily+ bars have no time-of-day to filter (Sprint 8 spec, section 12).
_SESSION_OPEN_TIME = pd.Timestamp("09:30").time()
_SESSION_CLOSE_TIME = pd.Timestamp("16:00").time()

_INTRADAY_INTERVALS = {
    Interval.MINUTE_1,
    Interval.MINUTE_5,
    Interval.MINUTE_15,
    Interval.MINUTE_30,
    Interval.HOUR_1,
}

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


@dataclass(frozen=True)
class _ResolvedRequest:
    symbol: str
    start: date
    end: date
    interval: Interval


class MarketDataService:
    """The single entry point for historical candle data.

    Fetching, caching, canonicalization, validation, and session
    filtering are deliberately separate responsibilities (Sprint 8's
    architecture diagram, `DECISIONS.md` ADR-0041):
    `CacheManager` (`src/utils/cache.py`) only knows how to read/write a
    DataFrame under a string key; `canonicalize_candles`
    (`src/data/canonical.py`) only knows how to make a DataFrame's
    shape/dtype/timezone deterministic; `validate_candles`
    (`src/data/validation.py`) only knows how to check a candle
    DataFrame's contract and quality; `TradingCalendar`
    (`src/calendar/`) only knows what a trading session is. This class
    orchestrates all four into "give me trustworthy candles."

    Args:
        provider: the DataProvider to use. Defaults to Yahoo Finance.
        cache_dir: directory for on-disk caching. Defaults to data/cache.
            Ignored if `cache` is provided.
        use_cache: whether to read/write the cache by default. Can be
            overridden per-call via `get_candles(..., use_cache=...)`.
        cache: an explicit CacheManager to use instead of constructing
            one from `cache_dir`. Mainly useful for tests or for sharing
            one CacheManager instance across services.
        calendar: the `TradingCalendar` used for session filtering and
            gap detection. Defaults to `NYSECalendar()` -- the only
            market this platform trades against today.
    """

    def __init__(
        self,
        provider: DataProvider | None = None,
        cache_dir: Path | str | None = None,
        use_cache: bool = True,
        cache: CacheManager | None = None,
        calendar: TradingCalendar | None = None,
    ) -> None:
        self._provider = provider or YFinanceProvider()
        self._cache = cache or CacheManager(cache_dir or DEFAULT_CACHE_DIR)
        self._use_cache = use_cache
        self._calendar = calendar or NYSECalendar()

    def get_candles(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        interval: Interval | str = Interval.DAY_1,
        use_cache: bool | None = None,
        session: SessionPolicy = SessionPolicy.REGULAR,
    ) -> pd.DataFrame:
        """Return OHLCV candles for `symbol`.

        Args:
            symbol: ticker symbol, e.g. "SPY".
            start: first day to include. Defaults to one year before `end`.
            end: last day to include. Defaults to today.
            interval: candle size. Defaults to daily.
            use_cache: overrides the service-level cache setting for this call.
            session: which candles to include (`DECISIONS.md`, ADR-0041).
                `SessionPolicy.REGULAR` (default) filters intraday
                candles to the regular 09:30-16:00 America/New_York
                session; a no-op for daily+ intervals. `SessionPolicy.ALL`
                returns whatever was fetched, unfiltered.

        Returns:
            A DataFrame indexed by a timezone-aware, UTC "timestamp"
            DatetimeIndex, columns open/high/low/close/volume -- sorted
            ascending, no duplicates, validated
            (`src.data.validation.validate_candles`).

        Raises:
            ValueError: symbol is empty, or start is after end.
            NoDataError: the request was valid but no data was available
                (including: valid data existed but none of it survived
                session filtering).
            DataProviderError: the underlying provider failed.
            DataValidationError: the resulting dataset failed structural
                or financial-sanity validation.
        """
        candles, _ = self._resolve(symbol, start, end, interval, use_cache, session)
        return candles

    def get_history(
        self,
        symbol: str,
        period: str = DEFAULT_PERIOD,
        interval: Interval | str = DEFAULT_INTERVAL,
        end: date | None = None,
        use_cache: bool | None = None,
        session: SessionPolicy = SessionPolicy.REGULAR,
    ) -> pd.DataFrame:
        """Convenience wrapper for get_candles using a period string.

        Same result as `get_candles`, but takes a yfinance-style relative
        period (e.g. "5d", "3wk", "6mo", "2y") instead of an explicit
        `start` date. Defaults to the watchlist-wide settings in
        `src/config/settings.py` (DEFAULT_PERIOD, DEFAULT_INTERVAL) so
        callers that don't care about the specifics get sane values for
        free.

        Raises:
            ValueError: `period` is malformed, symbol is empty, or the
                resulting date range is invalid.
            NoDataError: the request was valid but no data was available.
            DataProviderError: the underlying provider failed.
            DataValidationError: the resulting dataset failed validation.
        """
        end = end or date.today()
        start = period_to_start(period, end)
        return self.get_candles(
            symbol, start=start, end=end, interval=interval,
            use_cache=use_cache, session=session,
        )

    def get_dataset(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        interval: Interval | str = Interval.DAY_1,
        use_cache: bool | None = None,
        session: SessionPolicy = SessionPolicy.REGULAR,
    ) -> CandleDataset:
        """Return a full `CandleDataset` -- candles plus the identity and
        session/quality metadata described in `src.data.models` (Sprint 8
        spec: the "Canonical Dataset" everything downstream should
        consume, `DECISIONS.md` ADR-0041).

        Same arguments and error conditions as `get_candles`.
        """
        candles, resolved = self._resolve(symbol, start, end, interval, use_cache, session)
        report = validate_candles(
            candles, symbol=resolved.symbol, interval=resolved.interval, calendar=self._calendar
        )
        return CandleDataset(
            symbol=resolved.symbol,
            interval=resolved.interval,
            provider=self._provider_name(),
            candles=candles,
            content_hash=dataframe_fingerprint(candles),
            validation_report=report,
            session_policy=session,
            session_timezone=self._calendar.local_timezone,
            requested_start=resolved.start,
            requested_end=resolved.end,
            dataset_start=candles.index.min(),
            dataset_end=candles.index.max(),
        )

    def _resolve(
        self,
        symbol: str,
        start: date | None,
        end: date | None,
        interval: Interval | str,
        use_cache: bool | None,
        session: SessionPolicy,
    ) -> tuple[pd.DataFrame, _ResolvedRequest]:
        symbol = self._validate_symbol(symbol)
        interval = Interval(interval)
        end = end or date.today()
        start = start or (end - timedelta(days=DEFAULT_LOOKBACK_DAYS))

        if start > end:
            raise ValueError(f"start ({start}) must not be after end ({end})")

        should_use_cache = self._use_cache if use_cache is None else use_cache
        merged = self._fetch_merged(symbol, start, end, interval, should_use_cache)

        # The full merged history is the trust gate (DECISIONS.md,
        # ADR-0041: "validation happens... whether freshly fetched or
        # served from cache") -- raises before anything bad is cached
        # or returned.
        validate_candles(merged, symbol=symbol, interval=interval, calendar=self._calendar)

        if should_use_cache:
            self._cache.set(self._cache_key(symbol, interval), merged)

        windowed = self._slice_to_range(merged, start, end)
        filtered = self._apply_session_policy(windowed, interval, session)

        if filtered.empty:
            raise NoDataError(
                f"No candles remain for {symbol} between {start} and {end} "
                f"at interval {interval.value} (session={session.value})"
            )

        return filtered, _ResolvedRequest(symbol, start, end, interval)

    def _fetch_merged(
        self, symbol: str, start: date, end: date, interval: Interval, should_use_cache: bool
    ) -> pd.DataFrame:
        """Return the full, canonicalized candle history available for
        `(symbol, interval)`, covering at least `[start, end]`.

        Incremental fetch (`DECISIONS.md`, ADR-0007, implemented under
        ADR-0041): a request whose `start` falls within already-cached
        data but whose `end` extends past it fetches only the new tail
        from the provider and merges it in, rather than re-fetching the
        whole range. A request whose `start` falls *before* the cached
        range falls back to a full refetch of `[start, end]` --
        deliberately not building general interval-merge logic for that
        less common case (Sprint 8 spec, section 9: "the architectural
        seam, not a full distributed data system").
        """
        cache_key = self._cache_key(symbol, interval)
        cached = self._cache.get(cache_key) if should_use_cache else None

        if cached is None or cached.empty:
            return canonicalize_candles(self._provider.fetch_candles(symbol, start, end, interval))

        cached = canonicalize_candles(cached)
        cached_start = cached.index.min().date()
        cached_end = cached.index.max().date()

        if start >= cached_start and end <= cached_end:
            return cached  # full hit, no fetch needed

        if start >= cached_start and end > cached_end:
            fetch_start = cached_end + timedelta(days=1)
            try:
                new_data = canonicalize_candles(
                    self._provider.fetch_candles(symbol, fetch_start, end, interval)
                )
            except NoDataError:
                return cached  # nothing new yet (e.g. no trading day since cached_end)
            merged = pd.concat([cached, new_data])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            return merged

        # start < cached_start: fall back to a full refetch of [start, end].
        return canonicalize_candles(self._provider.fetch_candles(symbol, start, end, interval))

    def _slice_to_range(self, df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
        start_ts = pd.Timestamp(start, tz="UTC")
        end_ts = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
        return df[(df.index >= start_ts) & (df.index < end_ts)]

    def _apply_session_policy(
        self, df: pd.DataFrame, interval: Interval, session: SessionPolicy
    ) -> pd.DataFrame:
        if session is SessionPolicy.ALL or interval not in _INTRADAY_INTERVALS or df.empty:
            return df

        local_index = df.index.tz_convert(self._calendar.local_timezone)
        in_hours = (local_index.time >= _SESSION_OPEN_TIME) & (
            local_index.time < _SESSION_CLOSE_TIME
        )
        session_days = pd.Series(local_index.date).map(self._calendar.is_session).to_numpy()
        return df[in_hours & session_days]

    def _provider_name(self) -> str:
        return getattr(self._provider, "name", type(self._provider).__name__)

    def _validate_symbol(self, symbol: str) -> str:
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol must be a non-empty string")
        return symbol

    def _cache_key(self, symbol: str, interval: Interval) -> str:
        return f"{symbol}_{interval.value}"
