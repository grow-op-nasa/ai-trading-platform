"""The Indicator Engine.

The only place indicators are calculated. Strategies ask this engine
for an indicator by name instead of computing it themselves, so every
strategy sees the exact same RSI, ATR, MACD, etc., computed the exact
same way -- no strategy-specific drift in how a "14-period RSI" is
defined.

    from src.indicators import IndicatorEngine

    engine = IndicatorEngine(candles)
    rsi = engine.calculate("RSI", period=14)
    vwap = engine.calculate("VWAP")
    atr = engine.calculate("ATR")
    macd = engine.calculate("MACD")

Adding a new indicator means writing a pure function in `formulas.py`
and decorating it with `@register_indicator("NAME")` -- this class
never needs to change.
"""

from __future__ import annotations

import pandas as pd

from src.data.base import REQUIRED_COLUMNS
from src.indicators import formulas  # noqa: F401 -- import registers indicators
from src.indicators.registry import available_indicators, get_indicator


class IndicatorEngine:
    """Computes registered indicators against one candles DataFrame.

    Args:
        candles: OHLCV DataFrame matching the `MarketDataService`
            contract (columns open/high/low/close/volume). Bound once
            at construction -- one engine per symbol/dataset, not a
            long-lived shared object across different candle sets.

    Raises:
        ValueError: `candles` is missing a required column.
    """

    def __init__(self, candles: pd.DataFrame) -> None:
        missing = [c for c in REQUIRED_COLUMNS if c not in candles.columns]
        if missing:
            raise ValueError(
                f"candles is missing required column(s): {missing}. "
                f"Expected all of: {REQUIRED_COLUMNS}"
            )
        self._candles = candles

    def calculate(self, name: str, **params) -> pd.Series | pd.DataFrame:
        """Calculate indicator `name` with the given parameters.

        Args:
            name: indicator name, case-insensitive (e.g. "RSI", "rsi").
            **params: keyword parameters forwarded to the indicator
                function (e.g. `period=14`).

        Returns:
            A Series (most indicators) or DataFrame (e.g. MACD's
            macd/signal/histogram columns), aligned to `candles`' index.

        Raises:
            UnknownIndicatorError: `name` isn't a registered indicator.
        """
        indicator_func = get_indicator(name)
        return indicator_func(self._candles, **params)

    @staticmethod
    def available_indicators() -> list[str]:
        """Names of every indicator this engine can calculate."""
        return available_indicators()
