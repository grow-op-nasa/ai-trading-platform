"""Controlled data-provider fault injection -- src/data/fault_injection.py (ADR-0051).

A SYNTHETIC test/integration seam, never used unless a caller constructs
it explicitly. It implements the ordinary `DataProvider` contract, so an
injected failure travels the same production path as a real one:

    FaultInjectingProvider -> MarketDataService (canonicalise, validate,
    session filter) -> monitoring observation -> checks -> findings.

What this proves: the platform handles *that class of failure* correctly.
What it does NOT prove: that a real third-party provider suffered an
outage. Every injected failure message starts with `[fault-injection]` so
the evidence persisted in findings is unmistakably synthetic.

`SyntheticCandleProvider` generates deterministic, financially valid
daily bars on real exchange sessions (via the calendar) so a "healthy"
or "stale" observation is produced by the real service rather than being
fabricated downstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

import pandas as pd

from src.calendar.base import TradingCalendar
from src.calendar.nyse import NYSECalendar
from src.data.base import REQUIRED_COLUMNS, DataProvider, Interval
from src.data.exceptions import DataProviderError, NoDataError

TAG = "[fault-injection]"


class FaultKind(str, Enum):
    TIMEOUT = "timeout"                      # DataProviderError, as a real provider wrapper raises
    CONNECTION = "connection"                # DataProviderError
    EMPTY = "empty"                          # NoDataError (valid request, nothing returned)
    MALFORMED_COLUMNS = "malformed_columns"  # response missing a required column
    IMPOSSIBLE_VALUES = "impossible_values"  # high < low: rejected by validation
    UNEXPECTED = "unexpected"                # non-contract exception (provider bug)
    STALE = "stale"                          # valid data that ends before `until`
    MISSING_BARS = "missing_bars"            # a bar missing mid-range -> coverage gap


@dataclass(frozen=True)
class Fault:
    kind: FaultKind
    detail: str = ""
    until: date | None = None  # STALE only: last bar date to keep


class SyntheticCandleProvider(DataProvider):
    """Deterministic daily candles on real exchange sessions up to
    `last_session` (inclusive). Prices are a simple deterministic ramp."""

    def __init__(self, last_session: date, calendar: TradingCalendar | None = None) -> None:
        self.last_session = last_session
        self._cal = calendar or NYSECalendar()

    def fetch_candles(self, symbol: str, start: date, end: date, interval: Interval) -> pd.DataFrame:
        if interval != Interval.DAY_1:
            raise DataProviderError(f"SyntheticCandleProvider only serves daily bars, got {interval.value}")
        days = self._cal.sessions_between(start, min(end, self.last_session))
        if not days:
            raise NoDataError(f"no sessions for {symbol} between {start} and {min(end, self.last_session)}")
        idx = pd.DatetimeIndex([pd.Timestamp(d) for d in days], name="timestamp")  # naive = UTC (daily convention)
        base = 100.0 + 0.1 * pd.Series(range(len(days)), index=idx, dtype=float)
        return pd.DataFrame(
            {"open": base, "high": base + 1.0, "low": base - 1.0, "close": base + 0.5, "volume": 1_000_000.0},
            index=idx,
        )[REQUIRED_COLUMNS]


class FaultInjectingProvider(DataProvider):
    """Wrap `base`; each `fetch_candles` call consumes the next entry of
    `script` (`None` = behave normally). The last entry repeats forever,
    so `[Fault(TIMEOUT)]` fails every call and `[None]` never fails.
    `calls` records every request for assertions."""

    def __init__(self, base: DataProvider, script: list[Fault | None]) -> None:
        if not script:
            raise ValueError("script must contain at least one entry")
        self._base = base
        self._script = list(script)
        self.calls: list[tuple[str, date, date, str]] = []

    def set_script(self, script: list[Fault | None]) -> None:
        if not script:
            raise ValueError("script must contain at least one entry")
        self._script = list(script)
        self._i = 0

    _i = 0

    def _next(self) -> Fault | None:
        fault = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return fault

    def fetch_candles(self, symbol: str, start: date, end: date, interval: Interval) -> pd.DataFrame:
        self.calls.append((symbol, start, end, interval.value))
        fault = self._next()
        if fault is None:
            return self._base.fetch_candles(symbol, start, end, interval)
        k = fault.kind
        if k == FaultKind.TIMEOUT:
            raise DataProviderError(f"{TAG} simulated provider timeout for {symbol} {fault.detail}".strip())
        if k == FaultKind.CONNECTION:
            raise DataProviderError(f"{TAG} simulated connection failure for {symbol} {fault.detail}".strip())
        if k == FaultKind.EMPTY:
            raise NoDataError(f"{TAG} simulated empty response for {symbol} {fault.detail}".strip())
        if k == FaultKind.UNEXPECTED:
            raise RuntimeError(f"{TAG} simulated provider bug for {symbol} {fault.detail}".strip())
        df = self._base.fetch_candles(symbol, start, end, interval)
        if k == FaultKind.MALFORMED_COLUMNS:
            return df.drop(columns=["volume"])
        if k == FaultKind.IMPOSSIBLE_VALUES:
            bad = df.copy()
            bad.iloc[-1, bad.columns.get_loc("high")] = bad.iloc[-1]["low"] - 5.0
            return bad
        if k == FaultKind.MISSING_BARS:
            return df.drop(df.index[len(df) // 2])
        if k == FaultKind.STALE:
            if fault.until is None:
                raise ValueError("STALE fault needs `until`")
            kept = df[df.index.date <= fault.until]
            if kept.empty:
                raise NoDataError(f"{TAG} stale fault left no bars for {symbol}")
            return kept
        raise ValueError(f"unknown fault kind {k!r}")
