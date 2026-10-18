"""Strategy history requirements -- src/strategies/history.py (Sprint 22, ADR-0059).

A strategy DECLARES how much history it needs to make a valid decision; the platform never infers that from parameter
names or generic multipliers. The declaration is a typed, parameter-aware value object returned by the strategy's
`history_requirement()` method, so `EMACrossStrategy(fast=8, slow=21)` and `EMACrossStrategy(fast=50, slow=200)` declare
different requirements.

Semantics (the single definition every consumer shares)
-------------------------------------------------------
`min_bars` is the minimum number of CONSECUTIVE, COMPLETE bars of the evaluation's bar size (the scanner and paper
runtime use daily bars), ending at and including the decision bar, that must be present in the evaluated window for the
strategy's decision at that bar to be treated as valid. Fewer bars means the strategy has not seen enough history; the
outcome is NOT_EVALUABLE, never "no signal".

It is a data-sufficiency bound, not a promise of window independence: the shipped strategies keep an `in_position` state
across the window, so a decision can still depend on earlier bars than `min_bars`. The platform therefore pins the
evaluation window (lookback) and stores the exact bars so a decision can be reproduced (see `src.snapshots`).

How the built-in requirements are derived (so they can be tested, not trusted)
-----------------------------------------------------------------------------
* EMA(span) (`ewm(span, adjust=False)`): defined from the first bar and converging towards the true value; the first
  bar's residual weight after k further bars is (1 - 2/(span+1))**k. A bar count is "warmed up" when that residual is at
  most `SEED_WEIGHT_TOLERANCE` (1%). bars = k + 1.
* RSI(period) (`ewm(alpha=1/period)` on diff gains/losses): the same convergence rule with alpha = 1/period, plus one bar
  because the first difference needs two prices. bars = k + 2.
* ATR(period) (`rolling(period).mean()` of true range): the first defined value needs `period` bars.
* Rolling windows: `window` bars. Percentage change over n bars: n + 1 bars.
* A strategy built from several indicators needs the MAXIMUM of its components (they are computed over the same window).

`HistoryRequirement.components` records each contributing term and its basis, so the explanation shown to users is the
actual derivation. A strategy that declares nothing, or declares something invalid, has an UNDECLARED / INVALID lookup
status and is not scannable, evaluable in the paper runtime, or promotable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: Residual weight of the first observation tolerated for an exponentially smoothed indicator.
SEED_WEIGHT_TOLERANCE = 0.01
#: No declaration may exceed this (a guard against nonsense such as 10**9, not a policy cap on real strategies).
MAX_DECLARABLE_BARS = 5000
UNIT = "complete_bars"
FORMAT_VERSION = 1

SEMANTICS = (
    "Minimum number of consecutive complete bars, ending at the decision bar, that the strategy must be given for its "
    "decision at that bar to count. With fewer bars the result is NOT_EVALUABLE, never NO_SIGNAL."
)

DECLARED = "DECLARED"
UNDECLARED = "UNDECLARED"
INVALID = "INVALID"


class HistoryRequirementError(ValueError):
    """A history requirement is malformed."""


@dataclass(frozen=True)
class HistoryComponent:
    """One term of a requirement: `bars` needed because of `name` (for example 'EMA(21)'), with the derivation in `basis`."""

    name: str
    bars: int
    basis: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise HistoryRequirementError("a history component needs a name")
        _check_bars(self.bars, f"component {self.name!r}")
        if not isinstance(self.basis, str) or not self.basis:
            raise HistoryRequirementError(f"component {self.name!r} needs a basis (how the bar count was derived)")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "bars": self.bars, "basis": self.basis}


@dataclass(frozen=True)
class HistoryRequirement:
    """Typed, validated history declaration. `min_bars` must be at least the largest component."""

    min_bars: int
    components: tuple[HistoryComponent, ...]
    unit: str = UNIT
    version: int = FORMAT_VERSION

    def __post_init__(self) -> None:
        _check_bars(self.min_bars, "min_bars")
        if self.unit != UNIT:
            raise HistoryRequirementError(f"unsupported history unit {self.unit!r}")
        if self.version != FORMAT_VERSION:
            raise HistoryRequirementError(f"unsupported history requirement version {self.version!r}")
        if not self.components or not all(isinstance(c, HistoryComponent) for c in self.components):
            raise HistoryRequirementError("a history requirement needs at least one component explaining it")
        if self.min_bars < max(c.bars for c in self.components):
            raise HistoryRequirementError("min_bars is smaller than its largest component")

    def explain(self) -> str:
        parts = "; ".join(f"{c.name}: {c.bars} ({c.basis})" for c in self.components)
        return f"{self.min_bars} complete bars required -- the largest of: {parts}."

    def to_dict(self) -> dict[str, Any]:
        return {"min_bars": self.min_bars, "unit": self.unit, "version": self.version,
                "components": [c.to_dict() for c in self.components]}

    @classmethod
    def from_dict(cls, raw: Any) -> "HistoryRequirement":
        if not isinstance(raw, dict):
            raise HistoryRequirementError("a history requirement must be an object")
        try:
            comps = tuple(HistoryComponent(c["name"], c["bars"], c["basis"]) for c in raw["components"])
            return cls(raw["min_bars"], comps, raw.get("unit", UNIT), raw.get("version", FORMAT_VERSION))
        except HistoryRequirementError:
            raise
        except (KeyError, TypeError) as exc:
            raise HistoryRequirementError(f"malformed history requirement: {type(exc).__name__}: {exc}") from exc


def _check_bars(value: Any, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoryRequirementError(f"{what} must be an integer number of bars, got {value!r}")
    if not 1 <= value <= MAX_DECLARABLE_BARS:
        raise HistoryRequirementError(f"{what} must be between 1 and {MAX_DECLARABLE_BARS}, got {value}")


def _period(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HistoryRequirementError(f"{what} must be a positive integer, got {value!r}")
    return value


# ------------------------------------------------------------------------------------------------ derivations
def _bars_to_converge(alpha: float) -> int:
    """Smallest k with (1 - alpha)**k <= SEED_WEIGHT_TOLERANCE."""
    if alpha >= 1.0:
        return 0
    return math.ceil(math.log(SEED_WEIGHT_TOLERANCE) / math.log(1.0 - alpha))


def ema_bars(span: int) -> int:
    _period(span, "EMA span")
    return _bars_to_converge(2.0 / (span + 1)) + 1


def rsi_bars(period: int) -> int:
    _period(period, "RSI period")
    return _bars_to_converge(1.0 / period) + 2


def atr_bars(period: int) -> int:
    return _period(period, "ATR period")


def rolling_bars(window: int) -> int:
    return _period(window, "rolling window")


def pct_change_bars(n: int) -> int:
    return _period(n, "return period") + 1


def macd_bars(fast: int, slow: int, signal: int) -> int:
    """MACD = EMA(fast)-EMA(slow), then an EMA(signal) of that line: the signal EMA starts once the line is defined."""
    return max(ema_bars(fast), ema_bars(slow)) + ema_bars(signal) - 1


def component(name: str, bars: int, basis: str) -> HistoryComponent:
    return HistoryComponent(name, bars, basis)


def ema_component(span: int) -> HistoryComponent:
    return component(f"EMA({span})", ema_bars(span), f"first-bar weight <= {SEED_WEIGHT_TOLERANCE:.0%} after {ema_bars(span) - 1} bars")


def rsi_component(period: int) -> HistoryComponent:
    return component(f"RSI({period})", rsi_bars(period),
                     f"first difference needs 2 prices, smoothing converges to <= {SEED_WEIGHT_TOLERANCE:.0%} seed weight")


def atr_component(period: int) -> HistoryComponent:
    return component(f"ATR({period})", atr_bars(period), f"rolling mean of true range over {period} bars")


def rolling_component(label: str, window: int) -> HistoryComponent:
    return component(f"{label}({window})", rolling_bars(window), f"rolling window of {window} bars")


def pct_change_component(n: int) -> HistoryComponent:
    return component(f"return({n})", pct_change_bars(n), f"{n}-bar change needs {n + 1} prices")


def macd_component(fast: int, slow: int, signal: int) -> HistoryComponent:
    return component(f"MACD({fast},{slow},{signal})", macd_bars(fast, slow, signal),
                     "slow EMA converges, then the signal EMA of the MACD line converges")


def combine(*components: HistoryComponent) -> HistoryRequirement:
    """Requirement whose `min_bars` is the largest component."""
    if not components:
        raise HistoryRequirementError("combine() needs at least one component")
    return HistoryRequirement(max(c.bars for c in components), tuple(components))


# ------------------------------------------------------------------------------------------------ lookup / assessment
@dataclass(frozen=True)
class RequirementLookup:
    """What a strategy instance declares. `status` is DECLARED, UNDECLARED or INVALID; only DECLARED has a requirement."""

    status: str
    requirement: HistoryRequirement | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.status == DECLARED and self.requirement is not None


def requirement_of(strategy: Any) -> RequirementLookup:
    """Ask the instance for its declaration. Never guesses: a missing or broken declaration is reported as such."""
    method = getattr(strategy, "history_requirement", None)
    if not callable(method):
        return RequirementLookup(UNDECLARED, None, "the strategy declares no history requirement (no history_requirement())")
    try:
        value = method()
    except Exception as exc:  # noqa: BLE001 - a broken declaration is reported, never trusted
        return RequirementLookup(INVALID, None, f"history_requirement() raised {type(exc).__name__}: {exc}")
    if not isinstance(value, HistoryRequirement):
        return RequirementLookup(INVALID, None, f"history_requirement() returned {type(value).__name__}, not a HistoryRequirement")
    return RequirementLookup(DECLARED, value, "")


@dataclass(frozen=True)
class HistoryAssessment:
    """Requirement versus the complete bars actually available -- the record persisted with a scan result."""

    status: str                      # DECLARED | UNDECLARED | INVALID
    required: int | None
    available: int
    satisfied: bool
    shortfall: int = 0
    explanation: str = ""
    requirement: dict[str, Any] | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "required": self.required, "available": self.available, "satisfied": self.satisfied,
                "shortfall": self.shortfall, "explanation": self.explanation, "requirement": self.requirement,
                "semantics": SEMANTICS}


def assess_history(lookup: RequirementLookup, available: int) -> HistoryAssessment:
    """Pure comparison. An undeclared/invalid requirement is NEVER satisfied."""
    if not lookup.usable:
        return HistoryAssessment(lookup.status, None, int(available), False, 0, lookup.detail, None)
    req = lookup.requirement
    ok = available >= req.min_bars
    return HistoryAssessment(DECLARED, req.min_bars, int(available), ok, 0 if ok else req.min_bars - int(available),
                             req.explain(), req.to_dict())


def assess_strategy(strategy: Any, available: int) -> HistoryAssessment:
    return assess_history(requirement_of(strategy), available)
