"""The Strategy SDK.

Writing a strategy from scratch means re-deriving the same boilerplate
every time: build an `IndicatorEngine`, validate the input columns, tag
log lines with which strategy they came from, remember `Signal`'s exact
field names. `BaseStrategy` provides all of that -- it does not decide
when to emit a signal, what direction to choose, or how confident to
be. That decision-making stays entirely with the strategy author, in
their own `prepare()`/`generate_signals()` (see `DECISIONS.md`,
ADR-0018).

`Strategy` (`src/strategies/base.py`) is unchanged: still a `Protocol`,
satisfied structurally. `BaseStrategy` is one convenient way to satisfy
it, not a requirement -- a strategy that doesn't need any of these
helpers can still just implement the three methods directly, exactly
as before.

    class EMACrossStrategy(BaseStrategy):
        def __init__(self, fast: int = 20, slow: int = 50):
            super().__init__(name="ema_cross")
            self._fast = fast
            self._slow = slow

        def prepare(self, data):
            self.require_columns(data, "close")
            out = data.copy()
            out["ema_fast"] = self.indicator(data, "EMA", period=self._fast)
            out["ema_slow"] = self.indicator(data, "EMA", period=self._slow)
            return out

        def generate_signals(self, data):
            signals = []
            in_position = False
            for timestamp, row in data.iterrows():
                if pd.isna(row["ema_fast"]) or pd.isna(row["ema_slow"]):
                    continue
                crossed_up = row["ema_fast"] > row["ema_slow"]
                if crossed_up and not in_position:
                    signals.append(self.emit_signal(
                        timestamp, SignalDirection.LONG, confidence=0.7,
                        reason="EMA fast crossed above EMA slow",
                    ))
                    in_position = True
                elif not crossed_up and in_position:
                    signals.append(self.emit_signal(
                        timestamp, SignalDirection.FLAT, confidence=0.7,
                        reason="EMA fast crossed below EMA slow",
                    ))
                    in_position = False
            return signals
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd
from loguru import logger

from src.data.base import REQUIRED_COLUMNS
from src.indicators.engine import IndicatorEngine
from src.signals.models import Signal, SignalDirection


class BaseStrategy(ABC):
    """Optional base class handling the mechanical parts of a strategy.

    Subclasses still implement `prepare()` and `generate_signals()` in
    full -- including deciding when and how often to emit a signal.
    Nothing here makes that decision on a strategy's behalf.

    Args:
        name: short, human-readable identifier for this strategy.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self.log = logger.bind(strategy=name)

    @property
    def name(self) -> str:
        return self._name

    @abstractmethod
    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Enrich `data` with whatever this strategy needs to decide.

        Typically calls `self.indicator(...)` to add columns. Must not
        mutate `data` in place.
        """
        raise NotImplementedError

    @abstractmethod
    def generate_signals(self, data: pd.DataFrame) -> list[Signal]:
        """Decide what, if anything, changed about the desired position.

        Returns a sparse `list[Signal]` -- one per decision point, not
        one per row. Typically built with repeated calls to
        `self.emit_signal(...)`.
        """
        raise NotImplementedError

    def indicator(self, data: pd.DataFrame, name: str, **params):
        """Shorthand for `IndicatorEngine(data).calculate(name, **params)`.

        Stateless -- built fresh from whichever `data` is passed in, so
        the same strategy instance can be reused safely across multiple
        backtest runs on different candle sets.
        """
        return IndicatorEngine(data).calculate(name, **params)

    def require_columns(self, data: pd.DataFrame, *columns: str) -> None:
        """Raise a clear, strategy-attributed error if `data` is missing
        any of `columns`. Call with no arguments beyond `data` to check
        the standard OHLCV contract (`REQUIRED_COLUMNS`).

        Raises:
            ValueError: one or more columns are missing.
        """
        expected = columns or tuple(REQUIRED_COLUMNS)
        missing = [c for c in expected if c not in data.columns]
        if missing:
            raise ValueError(f"{self.name}: missing required column(s): {missing}")

    def emit_signal(
        self,
        timestamp: pd.Timestamp,
        direction: SignalDirection,
        confidence: float,
        **metadata,
    ) -> Signal:
        """Construct, log, and return one `Signal`.

        Automatically merges `{"strategy": self.name}` into `metadata`
        (author-supplied keys win on collision) so a signal remains
        traceable back to its origin even when inspected independently
        of the `BacktestResult`/experiment it came from.
        """
        merged_metadata = {"strategy": self.name, **metadata}
        signal = Signal(
            timestamp=timestamp,
            direction=direction,
            confidence=confidence,
            metadata=merged_metadata,
        )
        self.log.debug(
            "Signal: {} {} confidence={} {}",
            timestamp,
            direction.value,
            confidence,
            merged_metadata,
        )
        return signal
