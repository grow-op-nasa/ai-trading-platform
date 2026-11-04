"""Strategy catalogue -- family taxonomy and descriptive metadata for every registered strategy (Sprint 23, ADR-0061).

Metadata is *descriptive*: it never changes what a strategy does. It lets research and the UI group strategies by
the market hypothesis they test, so a new strategy appears in every view by declaring itself, not by editing a
hard-coded UI mapping.

A strategy declares itself by setting a class attribute ``METADATA = StrategyMetadata(...)``. The three strategies
that predate this module (and ``ai_signal``) are described in ``_BUILT_IN_METADATA`` below instead: adding a class
attribute to them would change their source hash (``src.strategies.identity.strategy_version``) and therefore the
identity under which earlier experiments were recorded, for no behavioural reason. A registered strategy with no
metadata at all is reported as ``UNCLASSIFIED`` -- never guessed -- and a test fails until it is described.

Two things are deliberately *not* stored here: implementation maturity (derived from the promotion manifest) and
research maturity (derived from stored research runs, ``src.strategy_research``). Neither can be claimed by a
strategy about itself.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.data.base import Interval


class StrategyFamily(str, Enum):
    TREND_FOLLOWING = "TREND_FOLLOWING"
    MOMENTUM = "MOMENTUM"
    MEAN_REVERSION = "MEAN_REVERSION"
    BREAKOUT = "BREAKOUT"
    RELATIVE_STRENGTH = "RELATIVE_STRENGTH"      # reserved: needs cross-sectional data, not implemented
    VOLATILITY_REGIME = "VOLATILITY_REGIME"      # only for strategies whose edge hypothesis is the regime itself
    MODEL_DRIVEN = "MODEL_DRIVEN"                # hypothesis is learned by a model, not declared
    UNCLASSIFIED = "UNCLASSIFIED"


class Directionality(str, Enum):
    LONG_FLAT = "LONG_FLAT"
    LONG_SHORT = "LONG_SHORT"


class ImplementationMaturity(str, Enum):
    REGISTERED = "REGISTERED"    # built in and registered in the production registry
    PROMOTED = "PROMOTED"        # a human-approved candidate registered through the promotion workflow


@dataclass(frozen=True)
class StrategyMetadata:
    family: StrategyFamily
    hypothesis: str
    entry: str
    exit: str
    directionality: Directionality
    supported_intervals: tuple[str, ...]
    regime_assumptions: str
    known_weaknesses: tuple[str, ...]
    display_name: str | None = None
    variant_of: str | None = None     # set when the hypothesis is not independent of another strategy
    researchable: bool = True         # False when the strategy cannot be built from plain parameters alone
    researchable_note: str = ""


_DAILY = (Interval.DAY_1.value,)

_BUILT_IN_METADATA: dict[str, StrategyMetadata] = {
    "ema_cross": StrategyMetadata(
        family=StrategyFamily.TREND_FOLLOWING,
        display_name="EMA crossover",
        hypothesis="A faster EMA above a slower EMA marks an up-trend that tends to persist.",
        entry="Flat and EMA(fast) > EMA(slow). State-based: it also enters on the first evaluable bar if the "
              "fast EMA is already above the slow one, without a fresh crossover.",
        exit="Long and EMA(fast) <= EMA(slow).",
        directionality=Directionality.LONG_FLAT,
        supported_intervals=_DAILY,
        regime_assumptions="Sustained directional markets; low reversal frequency.",
        known_weaknesses=(
            "Lags turning points and gives back a large part of each move.",
            "Whipsaws repeatedly in sideways markets, paying costs on each flip.",
            "A research window can begin in a position it did not enter by a crossover (first-bar state entry).",
        ),
    ),
    "ema_cross_vol_filter": StrategyMetadata(
        family=StrategyFamily.TREND_FOLLOWING,
        display_name="EMA crossover with volatility filter",
        hypothesis="The EMA trend signal is only worth acting on when volatility (ATR/close) is high enough.",
        entry="Flat, EMA(fast) > EMA(slow) and ATR(period)/close >= min_atr_pct.",
        exit="Long and either EMA(fast) <= EMA(slow) or ATR/close < min_atr_pct (an exit is never blocked).",
        directionality=Directionality.LONG_FLAT,
        supported_intervals=_DAILY,
        regime_assumptions="Trending markets with enough volatility for moves to exceed costs.",
        known_weaknesses=(
            "Not an independent hypothesis: it is ema_cross plus a filter, so results are highly correlated.",
            "The volatility threshold is an absolute level, so its meaning differs across instruments.",
            "The promotion evidence rested on very small samples (5 to 27 trades).",
        ),
        variant_of="ema_cross",
    ),
    "rsi_mean_reversion": StrategyMetadata(
        family=StrategyFamily.MEAN_REVERSION,
        display_name="RSI mean reversion",
        hypothesis="After an unusually fast decline (RSI oversold) price tends to rebound toward its recent mean.",
        entry="Flat and RSI(period) <= oversold (also on the first evaluable bar if already oversold).",
        exit="Long and RSI(period) >= overbought. There is no price stop.",
        directionality=Directionality.LONG_FLAT,
        supported_intervals=_DAILY,
        regime_assumptions="Range-bound or mildly trending markets in which declines are followed by recoveries.",
        known_weaknesses=(
            "Buys falling markets: in a persistent down-trend it enters early and can hold a large loss.",
            "No stop: the only exit is RSI reaching overbought, which may take a very long time.",
            "Few trades per instrument on a daily timeframe.",
        ),
    ),
    "ai_signal": StrategyMetadata(
        family=StrategyFamily.MODEL_DRIVEN,
        display_name="AI signal (trained model)",
        hypothesis="A trained classifier's predicted class and probability anticipate forward returns.",
        entry="Model predicts LONG/SHORT with probability >= min_probability.",
        exit="Model prediction changes direction or drops below the probability threshold.",
        directionality=Directionality.LONG_SHORT,
        supported_intervals=_DAILY,
        regime_assumptions="Whatever regime the model was trained on; not declared by the strategy.",
        known_weaknesses=(
            "Requires a previously trained, registered model (model_id), so it cannot be built from plain parameters.",
            "The hypothesis is learned, so it cannot be inspected the way a rule can.",
        ),
        researchable=False,
        researchable_note="Requires a trained model_id; not constructible from generic parameters.",
    ),
}

UNCLASSIFIED_METADATA = StrategyMetadata(
    family=StrategyFamily.UNCLASSIFIED,
    hypothesis="Not described.",
    entry="Not described.",
    exit="Not described.",
    directionality=Directionality.LONG_FLAT,
    supported_intervals=(),
    regime_assumptions="Not described.",
    known_weaknesses=("No metadata has been declared for this strategy.",),
)


def metadata_for(strategy_name: str) -> StrategyMetadata:
    """Declared metadata: the class attribute first, then the built-in table, else ``UNCLASSIFIED_METADATA``."""
    from src.strategies.registry import get_strategy_class

    cls = get_strategy_class(strategy_name)
    declared = getattr(cls, "METADATA", None)
    if isinstance(declared, StrategyMetadata):
        return declared
    return _BUILT_IN_METADATA.get(strategy_name, UNCLASSIFIED_METADATA)


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    default: Any
    annotation: str


@dataclass(frozen=True)
class CatalogueEntry:
    strategy_id: str
    name: str
    family: StrategyFamily
    metadata: StrategyMetadata
    parameters: tuple[ParameterSpec, ...]
    history_requirement: dict[str, Any] | None
    history_note: str
    maturity: ImplementationMaturity
    source_hash: str | None
    class_path: str
    extra: dict[str, Any] = field(default_factory=dict)


def _parameters(cls: type) -> tuple[ParameterSpec, ...]:
    out: list[ParameterSpec] = []
    try:
        for p in inspect.signature(cls.__init__).parameters.values():
            if p.name in ("self", "symbol") or p.default is inspect.Parameter.empty:
                continue
            ann = "" if p.annotation is inspect.Parameter.empty else str(getattr(p.annotation, "__name__", p.annotation))
            out.append(ParameterSpec(p.name, p.default, ann))
    except (TypeError, ValueError):
        pass
    return tuple(out)


def _promoted_names() -> set[str]:
    try:
        from src.strategies.promoted import read_manifest

        return {e.strategy_name for e in read_manifest()}
    except Exception:  # noqa: BLE001 - an unreadable manifest means nothing is claimed as promoted
        return set()


def build_catalogue() -> list[CatalogueEntry]:
    """One entry per registered strategy, sorted by id. Never runs a strategy or touches market data."""
    import src.strategies  # noqa: F401  registers the built-in and promoted strategies
    from src.strategies.identity import strategy_version
    from src.strategies.registry import available_strategies, get_strategy_class

    promoted = _promoted_names()
    entries: list[CatalogueEntry] = []
    for name in available_strategies():
        cls = get_strategy_class(name)
        meta = metadata_for(name)
        requirement, note = None, ""
        try:
            instance = cls(symbol="SPY")
            lookup = getattr(instance, "history_requirement", None)
            requirement = lookup().to_dict() if callable(lookup) else None
            note = "" if requirement is not None else "no history requirement declared"
        except Exception as exc:  # noqa: BLE001 - e.g. a strategy that needs constructor arguments
            note = f"not evaluated with default parameters ({type(exc).__name__})"
        try:
            version = strategy_version(cls)
        except TypeError:
            version = None
        entries.append(CatalogueEntry(
            strategy_id=name,
            name=meta.display_name or name.replace("_", " ").title(),
            family=meta.family,
            metadata=meta,
            parameters=_parameters(cls),
            history_requirement=requirement,
            history_note=note,
            maturity=ImplementationMaturity.PROMOTED if name in promoted else ImplementationMaturity.REGISTERED,
            source_hash=version,
            class_path=f"{cls.__module__}.{cls.__qualname__}",
        ))
    return entries


def family_coverage(entries: list[CatalogueEntry] | None = None) -> dict[StrategyFamily, list[str]]:
    """Which strategy ids represent each family. Variants of another strategy are listed but marked ``(variant)``."""
    entries = entries if entries is not None else build_catalogue()
    out: dict[StrategyFamily, list[str]] = {f: [] for f in StrategyFamily}
    for e in entries:
        out[e.family].append(f"{e.strategy_id} (variant of {e.metadata.variant_of})" if e.metadata.variant_of else e.strategy_id)
    return out
