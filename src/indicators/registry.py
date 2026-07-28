"""The indicator registry.

Indicators register themselves here by name via `@register_indicator`.
`IndicatorEngine` (see `engine.py`) is the only thing that reads this
registry -- new indicators are added by writing a pure function and
decorating it, never by editing the engine itself.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd

IndicatorFunc = Callable[..., "pd.Series | pd.DataFrame"]

_REGISTRY: dict[str, IndicatorFunc] = {}


class UnknownIndicatorError(KeyError):
    """Raised when an indicator name isn't registered."""


def register_indicator(name: str) -> Callable[[IndicatorFunc], IndicatorFunc]:
    """Decorator that registers a function under `name` (case-insensitive).

    The decorated function must take a candles DataFrame as its first
    positional argument, plus any keyword parameters (e.g. `period=14`),
    and return a `pandas.Series` or `pandas.DataFrame` aligned to the
    candles' index.
    """

    def decorator(func: IndicatorFunc) -> IndicatorFunc:
        key = name.upper()
        if key in _REGISTRY:
            raise ValueError(f"Indicator '{name}' is already registered")
        _REGISTRY[key] = func
        return func

    return decorator


def get_indicator(name: str) -> IndicatorFunc:
    """Look up a registered indicator function by name.

    Raises:
        UnknownIndicatorError: no indicator is registered under `name`.
    """
    key = name.upper()
    if key not in _REGISTRY:
        raise UnknownIndicatorError(
            f"Unknown indicator '{name}'. Available: {available_indicators()}"
        )
    return _REGISTRY[key]


def available_indicators() -> list[str]:
    """Names of every currently registered indicator, sorted."""
    return sorted(_REGISTRY)
