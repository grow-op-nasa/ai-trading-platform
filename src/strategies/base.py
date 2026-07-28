"""The Strategy interface.

This is the seam the Backtesting Framework (`src/backtesting/`) is
built against. No concrete strategy exists yet -- the first one is
Sprint 3's deliberately simple EMA-cross/opening-range-breakout
strategy -- but the interface is defined now.

A `Strategy` is any object with this shape (structural typing via
`Protocol`, not inheritance -- a class doesn't need to subclass
anything to satisfy this interface, it just needs the right methods).

As of Sprint 3 (`DECISIONS.md`, ADR-0015, superseding ADR-0011),
`generate_signals()` returns a list of `Signal` objects (see
`src/signals/models.py`) rather than a DataFrame with a `signal`
column. Signals are emitted sparsely -- one per decision point, not
one per candle.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd

from src.signals.models import Signal


@runtime_checkable
class Strategy(Protocol):
    """The contract every strategy must satisfy.

    Strategies should never compute indicators themselves -- `prepare()`
    is expected to enrich `data` via `IndicatorEngine.calculate(...)`,
    not reimplement indicator math. Similarly, regime awareness (if a
    strategy wants it) comes from `MarketRegimeEngine`, not ad hoc logic.
    """

    @property
    def name(self) -> str:
        """A short, human-readable identifier for this strategy.

        Used in backtest reports and Experiment Registry entries -- it's
        how a strategy is referred to across the codebase, not just a
        variable name.
        """
        ...

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Enrich `data` with whatever the strategy needs to make decisions.

        Typically adds indicator columns (via `IndicatorEngine`) and/or
        regime columns (via `MarketRegimeEngine`) to a copy of `data`.
        Must not mutate `data` in place.

        Returns:
            A DataFrame aligned to `data`'s index, with additional
            columns as needed.
        """
        ...

    def generate_signals(self, data: pd.DataFrame) -> list[Signal]:
        """Decide what, if anything, changed about the desired position.

        Args:
            data: the output of this strategy's own `prepare()`.

        Returns:
            A list of `Signal` objects, one per decision point --
            not one per row of `data`. Order does not need to be
            pre-sorted; the Backtester sorts by `timestamp` itself.
            An empty list is valid (the strategy never saw a reason to
            change position, and stays FLAT throughout).
        """
        ...
