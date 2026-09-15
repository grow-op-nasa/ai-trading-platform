"""Strategy registry -- src/strategies/registry.py.

The mechanism for adding a new strategy without touching anything that
needs to look one up by name -- most importantly, `ExperimentSpec`
reconstruction (`src/experiments/spec.py`, `DECISIONS.md` ADR-0035).
Mirrors the same `@register_x` / lookup-by-name pattern already used by
`src/indicators/registry.py` and `src/cli/registry.py` -- one more
instance of an established idea, not a new one.

    from src.strategies.registry import register_strategy

    @register_strategy("ema_cross")
    class EMACrossStrategy(BaseStrategy):
        ...

Registration is purely additive: a strategy that never registers itself
still works everywhere it always did (`Backtester`, the Strategy SDK,
direct construction) -- it just can't be looked up by name for
`ExperimentSpec` reconstruction until it does.
"""

from __future__ import annotations

from typing import Callable, TypeVar

_REGISTRY: dict[str, type] = {}

_T = TypeVar("_T", bound=type)


def register_strategy(name: str) -> Callable[[_T], _T]:
    """Class decorator registering a strategy class under `name`.

    Raises:
        ValueError: `name` is already registered (to a different class,
            or the same one re-decorated) -- fails loudly rather than
            silently letting one registration shadow another.
    """

    def decorator(strategy_cls: _T) -> _T:
        if name in _REGISTRY:
            raise ValueError(
                f"a strategy is already registered under {name!r}: "
                f"{_REGISTRY[name]!r}"
            )
        _REGISTRY[name] = strategy_cls
        return strategy_cls

    return decorator


def get_strategy_class(name: str) -> type:
    """The class registered under `name`.

    Raises:
        KeyError: nothing is registered under `name`.
    """
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(
            f"no strategy registered under {name!r} -- available: "
            f"{available_strategies()}"
        ) from exc


def available_strategies() -> list[str]:
    """Every registered strategy name, sorted."""
    return sorted(_REGISTRY)
