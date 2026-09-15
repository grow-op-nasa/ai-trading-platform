"""RSI Mean Reversion -- the platform's second permanent strategy.

Built for exactly one reason (`ROADMAP.md`, Sprint 6 close-out item):
prove that a second, genuinely different strategy plugs into the
existing research pipeline as an extension, not a rewrite. Nothing in
`src/backtesting`, `src/experiments`, `src/attribution`, `src/research`,
or `src/broker` changed, or needed to change, to add this file
(`tests/test_architecture.py` asserts this directly).

Long-only, like `EMACrossStrategy`: go LONG when RSI drops into oversold
territory (the market has fallen further/faster than a mean-reversion
thesis expects), go FLAT once RSI recovers into overbought territory (the
reversion the strategy was betting on has played out, or reversed against
it -- either way, the thesis is no longer live). Deliberately the
opposite trading idea from `EMACrossStrategy` (reversion vs. trend
following), not just a parameter variant of it -- a real second data
point for whether the platform's abstractions (Strategy SDK, Signal,
Backtester, Risk, Execution, Attribution, Research) generalize.
"""

from __future__ import annotations

import pandas as pd

from src.signals.models import Signal, SignalDirection
from src.strategies.registry import register_strategy
from src.strategies.sdk import BaseStrategy

DEFAULT_PERIOD = 14
DEFAULT_OVERSOLD = 30.0
DEFAULT_OVERBOUGHT = 70.0
DEFAULT_CONFIDENCE = 0.6


@register_strategy("rsi_mean_reversion")
class RSIMeanReversionStrategy(BaseStrategy):
    """Long while RSI is recovering from oversold, flat once it reaches
    overbought.

    Enters LONG the first time RSI drops to or below `oversold`; exits to
    FLAT the first time RSI, while in that position, rises to or above
    `overbought`. Emits a `Signal` only at those transitions -- sparse by
    construction (ADR-0015), not one signal per candle.

    Args:
        symbol: which instrument this strategy instance decides for
            (`DECISIONS.md`, ADR-0033) -- attached to every `Signal` it
            emits via `BaseStrategy.emit_signal()`.
        period: RSI lookback period (`src.indicators`'s `"RSI"`).
        oversold: RSI threshold at or below which the strategy enters
            LONG. Must be less than `overbought`.
        overbought: RSI threshold at or above which the strategy exits
            an open LONG to FLAT. Must be greater than `oversold`.
        confidence: fixed confidence attached to every signal this
            strategy emits -- like `EMACrossStrategy`, there's no natural
            continuous confidence measure for a threshold rule this
            simple, so it's a constant rather than a fabricated score.

    Raises:
        ValueError: `oversold` is not strictly less than `overbought`, or
            either is outside `(0, 100)`.
    """

    def __init__(
        self,
        symbol: str,
        period: int = DEFAULT_PERIOD,
        oversold: float = DEFAULT_OVERSOLD,
        overbought: float = DEFAULT_OVERBOUGHT,
        confidence: float = DEFAULT_CONFIDENCE,
    ) -> None:
        if not 0 < oversold < overbought < 100:
            raise ValueError(
                f"oversold ({oversold}) must be less than overbought "
                f"({overbought}), and both must be in (0, 100)"
            )
        super().__init__(name="rsi_mean_reversion", symbol=symbol)
        self._period = period
        self._oversold = oversold
        self._overbought = overbought
        self._confidence = confidence

    @property
    def params(self) -> dict:
        """`period`/`oversold`/`overbought`/`confidence` -- everything
        `__init__` needs besides `symbol` to reconstruct an equivalent
        instance. Read by `ExperimentSpec.capture()` (`DECISIONS.md`,
        ADR-0035)."""
        return {
            "period": self._period,
            "oversold": self._oversold,
            "overbought": self._overbought,
            "confidence": self._confidence,
        }

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        self.require_columns(data)
        out = data.copy()
        out["rsi"] = self.indicator(data, "RSI", period=self._period)
        return out

    def generate_signals(self, data: pd.DataFrame) -> list[Signal]:
        signals: list[Signal] = []
        in_position = False
        for timestamp, row in data.iterrows():
            if pd.isna(row["rsi"]):
                continue  # indicator warmup -- no opinion yet
            if not in_position and row["rsi"] <= self._oversold:
                signals.append(
                    self.emit_signal(
                        timestamp,
                        SignalDirection.LONG,
                        confidence=self._confidence,
                        reason=(
                            f"RSI({self._period})={row['rsi']:.1f} at or below "
                            f"oversold threshold {self._oversold}"
                        ),
                    )
                )
                in_position = True
            elif in_position and row["rsi"] >= self._overbought:
                signals.append(
                    self.emit_signal(
                        timestamp,
                        SignalDirection.FLAT,
                        confidence=self._confidence,
                        reason=(
                            f"RSI({self._period})={row['rsi']:.1f} at or above "
                            f"overbought threshold {self._overbought}"
                        ),
                    )
                )
                in_position = False
        return signals
