"""EMA Crossover -- the platform's first permanent strategy.

Deliberately the simplest reasonable trading idea: long while the fast
EMA is above the slow EMA, flat otherwise. Chosen for that simplicity,
not for profitability (`ROADMAP.md`, Sprint 3 note): if the platform
can't explain why a strategy this simple wins or loses -- via
`PerformanceAttributor` and the AI Research Reporter -- it has no
chance of explaining anything more complex. This is a research vehicle
for exercising the platform end to end (Strategy SDK -> Backtester ->
Performance Attribution -> AI Research Reporter), not a strategy meant
to be traded as-is.

Registered under `"ema_cross"` via `@register_strategy`
(`src/strategies/registry.py`, `DECISIONS.md` ADR-0035) -- this is what
lets `ExperimentSpec.reconstruct_strategy()` rebuild an equivalent
instance from a stored experiment's `strategy_name` + `params` alone,
without this module needing to be imported by name anywhere else.
"""

from __future__ import annotations

import pandas as pd

from src.signals.models import Signal, SignalDirection
from src.strategies.registry import register_strategy
from src.strategies.sdk import BaseStrategy

DEFAULT_FAST_PERIOD = 12
DEFAULT_SLOW_PERIOD = 26
DEFAULT_CONFIDENCE = 0.7


@register_strategy("ema_cross")
class EMACrossStrategy(BaseStrategy):
    """Long while EMA(fast) > EMA(slow), flat otherwise.

    Long-only by design: a fast-below-slow state goes FLAT, not SHORT,
    which keeps both the entry logic and the evidence it produces
    (attribution, research reports) as simple as the strategy itself --
    shorting is a natural future variant, not added here. Emits a
    `Signal` only when the crossover state actually changes -- sparse
    by construction (ADR-0015), not one signal per candle.

    Args:
        symbol: which instrument this strategy instance decides for
            (`DECISIONS.md`, ADR-0033) -- attached to every `Signal` it
            emits via `BaseStrategy.emit_signal()`.
        fast: EMA period for the fast average.
        slow: EMA period for the slow average. Must be greater than
            `fast`, or the "cross" has no meaning.
        confidence: fixed confidence attached to every signal this
            strategy emits. A crossover either happened or it didn't --
            there's no natural continuous confidence measure for a
            strategy this simple, so it's a constant rather than a
            fabricated score.

    Raises:
        ValueError: `fast >= slow`.
    """

    def __init__(
        self,
        symbol: str,
        fast: int = DEFAULT_FAST_PERIOD,
        slow: int = DEFAULT_SLOW_PERIOD,
        confidence: float = DEFAULT_CONFIDENCE,
    ) -> None:
        if fast >= slow:
            raise ValueError(
                f"fast period ({fast}) must be less than slow period ({slow})"
            )
        super().__init__(name="ema_cross", symbol=symbol)
        self._fast = fast
        self._slow = slow
        self._confidence = confidence

    @property
    def params(self) -> dict:
        """`fast`/`slow`/`confidence` -- everything `__init__` needs
        besides `symbol` to reconstruct an equivalent instance. Read by
        `ExperimentSpec.capture()` (`DECISIONS.md`, ADR-0035)."""
        return {"fast": self._fast, "slow": self._slow, "confidence": self._confidence}

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        self.require_columns(data)
        out = data.copy()
        out["ema_fast"] = self.indicator(data, "EMA", period=self._fast)
        out["ema_slow"] = self.indicator(data, "EMA", period=self._slow)
        return out

    def generate_signals(self, data: pd.DataFrame) -> list[Signal]:
        signals: list[Signal] = []
        in_position = False
        for timestamp, row in data.iterrows():
            if pd.isna(row["ema_fast"]) or pd.isna(row["ema_slow"]):
                continue  # indicator warmup -- no opinion yet
            crossed_up = row["ema_fast"] > row["ema_slow"]
            if crossed_up and not in_position:
                signals.append(
                    self.emit_signal(
                        timestamp,
                        SignalDirection.LONG,
                        confidence=self._confidence,
                        reason=f"EMA({self._fast}) crossed above EMA({self._slow})",
                    )
                )
                in_position = True
            elif not crossed_up and in_position:
                signals.append(
                    self.emit_signal(
                        timestamp,
                        SignalDirection.FLAT,
                        confidence=self._confidence,
                        reason=f"EMA({self._fast}) crossed below EMA({self._slow})",
                    )
                )
                in_position = False
        return signals
