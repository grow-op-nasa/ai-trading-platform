"""Data shapes for the trustworthy-dataset boundary -- src/data/models.py.

Sprint 8 (`DECISIONS.md`, ADR-0041): the "Canonical Dataset" box in the
sprint's own architecture diagram. `MarketDataService.get_dataset()`
(added alongside the pre-existing `get_candles()`/`get_history()`, not
replacing them) is what actually produces a `CandleDataset` -- this
module only defines the shapes.

Plain dataclasses, no behavior beyond simple derived properties, the
same posture `src/backtesting/models.py` and `src/risk/models.py` take:
the *service* (`src/data/service.py`) and the *validation module*
(`src/data/validation.py`) do the actual work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

import pandas as pd

from src.data.base import Interval


class SessionPolicy(str, Enum):
    """Which candles a `MarketDataService` request should include.

    `REGULAR` is the default for strategy/backtest data (Sprint 8 spec,
    section 12): pre-market and after-hours candles are filtered out.
    `ALL` returns whatever the provider returned, unfiltered -- an
    explicit opt-out, not an accident of a provider that happens to
    include extended hours. There is deliberately no `PRE_MARKET` or
    `AFTER_HOURS` value yet: supporting those sessions is a documented
    future capability (`DECISIONS.md`, ADR-0041's non-goals), not
    something to half-implement behind a value nothing can select
    correctly yet.
    """

    REGULAR = "regular"
    ALL = "all"


@dataclass(frozen=True)
class GapReport:
    """Timestamps expected inside an open trading session but absent
    from the dataset -- distinct from an ordinary overnight/weekend/
    holiday gap, which is never included here (Sprint 8 spec, section
    5). Non-fatal: a `GapReport` is informational, attached to a
    `ValidationReport`, never raised.
    """

    missing_timestamps: tuple[pd.Timestamp, ...] = field(default_factory=tuple)

    @property
    def count(self) -> int:
        return len(self.missing_timestamps)

    @property
    def has_gaps(self) -> bool:
        return self.count > 0


@dataclass(frozen=True)
class ValidationReport:
    """The outcome of `src.data.validation.validate_candles()` on one
    dataset. Only ever returned for data that passed structural and
    financial-sanity validation -- a failure raises instead (see
    `src.data.exceptions.DataValidationError` and its two subclasses),
    so a caller holding a `ValidationReport` already knows the dataset
    is *valid*; `gaps`/`suspicious` describe *quality*, not validity.
    """

    symbol: str
    interval: Interval
    gaps: GapReport = field(default_factory=GapReport)
    suspicious: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_clean(self) -> bool:
        """True if the dataset has no detected gaps or suspicious
        records -- valid *and* unremarkable. False doesn't mean
        invalid; it means "look at `gaps`/`suspicious` before trusting
        this blindly for something gap-sensitive."""
        return not self.gaps.has_gaps and not self.suspicious


@dataclass(frozen=True)
class DatasetIdentity:
    """The minimal, portable identity of a dataset a backtest ran
    against -- symbol, timeframe, content hash, and session/timezone
    convention (Sprint 8 spec, section 11: "a backtest should be able
    to identify... dataset identity/hash"). Deliberately smaller than
    `CandleDataset` -- this is what travels with a `BacktestResult`,
    not the candles themselves (those already live in the dataset the
    backtest was given).
    """

    symbol: str
    interval: Interval
    content_hash: str
    session_policy: SessionPolicy
    timezone: str = "UTC"


@dataclass(frozen=True)
class CandleDataset:
    """A validated, canonicalized, identity-stamped set of candles --
    the "Canonical Dataset" a Sprint 8 `MarketDataService.get_dataset()`
    call produces, and the only form of market data anything downstream
    (research, backtests, strategies) should consume (Sprint 8's
    architectural invariant: "No strategy, backtest, or experiment
    consumes raw provider data directly").

    Args:
        symbol: the instrument this dataset covers.
        interval: the candle timeframe (`src.data.base.Interval`).
        provider: where the candles came from (e.g. `"yfinance"`).
        candles: the canonicalized, validated OHLCV DataFrame itself
            (`src.data.canonical.canonicalize_candles()`'s output) --
            timezone-aware UTC index, sorted ascending.
        content_hash: `src.utils.hashing.dataframe_fingerprint()` of
            `candles` -- the dataset identity seam (`DECISIONS.md`,
            ADR-0035, hardened by ADR-0041's canonicalization rules).
        validation_report: `src.data.validation.validate_candles()`'s
            report for `candles` -- gaps and suspicious-but-possible
            records, if any.
        session_policy: which session filter was applied to produce
            `candles` (`SessionPolicy.REGULAR` by default).
        session_timezone: the IANA timezone `session_policy` filtering
            was evaluated in (e.g. `"America/New_York"`) -- informational,
            not itself the timezone `candles`' index is expressed in
            (`timezone`, always `"UTC"`, is that).
        requested_start / requested_end: the range originally asked
            for -- may differ from the data actually returned (e.g. a
            requested start on a non-trading day).
        dataset_start / dataset_end: the timestamp of the first/last
            candle actually present in `candles`.
        timezone: the canonical internal timezone `candles`' index is
            expressed in. Always `"UTC"` (`DECISIONS.md`, ADR-0041) --
            present as an explicit field, not a hardcoded assumption
            callers have to already know, per the sprint's own
            "timezone/temporal convention" metadata requirement.
    """

    symbol: str
    interval: Interval
    provider: str
    candles: pd.DataFrame
    content_hash: str
    validation_report: ValidationReport
    session_policy: SessionPolicy
    session_timezone: str
    requested_start: date
    requested_end: date
    dataset_start: pd.Timestamp
    dataset_end: pd.Timestamp
    timezone: str = "UTC"

    @property
    def identity(self) -> DatasetIdentity:
        """The portable subset of this dataset's identity -- what a
        `Backtester` run should carry forward onto its `BacktestResult`
        (Sprint 8 spec, section 11)."""
        return DatasetIdentity(
            symbol=self.symbol,
            interval=self.interval,
            content_hash=self.content_hash,
            session_policy=self.session_policy,
            timezone=self.timezone,
        )
