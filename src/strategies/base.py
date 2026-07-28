"""The Strategy interface.

This is the seam the Backtesting Framework (`src/backtesting/`) is
built against. No concrete strategy exists yet -- that's Sprint 3 --
but the interface is defined now so Module 3 of the Sprint 2 research
engine has something real to run, not a placeholder that gets thrown
away once real strategies exist.

A `Strategy` is any object with this shape (structural typing via
`Protocol`, not inheritance -- a class doesn't need to subclass
anything to satisfy this interface, it just needs the right methods).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd

#: The column `generate_signals()` must include in its returned
#: DataFrame. Values are position targets: 1 (long), -1 (short), 0
#: (flat). Additional columns (e.g. `size`, `stop_loss`) are allowed and
#: ignored by the current Backtester -- this is the minimum contract.
SIGNAL_COLUMN = "signal"


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

    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Decide a position for every row of `data`.

        Args:
            data: the output of this strategy's own `prepare()`.

        Returns:
            A DataFrame aligned to `data`'s index containing at least
            the `SIGNAL_COLUMN` ("signal"), valued -1/0/1.
        """
        ...
