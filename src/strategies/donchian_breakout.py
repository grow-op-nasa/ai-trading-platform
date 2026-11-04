"""Donchian channel breakout -- the platform's first breakout-family strategy (Sprint 23, ADR-0061).

Hypothesis: when price closes beyond the range it has traded in over the last N bars, the move often
continues (range expansion, trend initiation). This is a different hypothesis from ``ema_cross`` (a smoothed
trend state) and from ``rsi_mean_reversion`` (a bet against extension): it enters only on a *new extreme*.

Rules (long/flat, completed bars only):

* Entry (while flat): ``close`` is strictly above the highest ``high`` of the previous ``entry_lookback`` bars.
* Exit (while long): ``close`` is strictly below the lowest ``low`` of the previous ``exit_lookback`` bars.
* The channel is computed from *prior* bars only (the decision bar is excluded), so the breakout bar never
  sets its own threshold. Equality with the boundary is not a break: both comparisons are strict.
* Signals are emitted only on a state change, like the other strategies. Short signals are never emitted.
"""

from __future__ import annotations

import pandas as pd

from src.data.base import Interval
from src.signals.models import Signal, SignalDirection
from src.strategies.catalogue import Directionality, StrategyFamily, StrategyMetadata
from src.strategies.history import HistoryRequirement, combine, component
from src.strategies.registry import register_strategy
from src.strategies.sdk import BaseStrategy

DEFAULT_ENTRY_LOOKBACK = 20
DEFAULT_EXIT_LOOKBACK = 10
DEFAULT_CONFIDENCE = 0.6


@register_strategy("donchian_breakout")
class DonchianBreakoutStrategy(BaseStrategy):
    """Long when the close breaks above the prior N-bar high; flat when it breaks below the prior M-bar low.

    Args:
        symbol: the instrument this instance decides for.
        entry_lookback: bars in the entry channel (prior bars, decision bar excluded). At least 2.
        exit_lookback: bars in the exit channel. At least 1 and not longer than ``entry_lookback``, so the
            exit is never slower than the entry (the classic asymmetric channel, e.g. 20 in / 10 out).
        confidence: fixed confidence attached to each signal. There is no natural continuous confidence for a
            threshold rule, so it is a constant, not a fabricated score.

    Raises:
        ValueError: invalid lookbacks.
    """

    METADATA = StrategyMetadata(
        family=StrategyFamily.BREAKOUT,
        hypothesis="A close beyond the prior N-bar range marks range expansion that tends to continue.",
        entry="Flat and close > highest high of the previous entry_lookback bars (strict).",
        exit="Long and close < lowest low of the previous exit_lookback bars (strict).",
        directionality=Directionality.LONG_FLAT,
        supported_intervals=(Interval.DAY_1.value,),
        regime_assumptions="Trending or range-expanding markets; volatility expansion after consolidation.",
        known_weaknesses=(
            "Whipsaws in range-bound markets: false breakouts are entered and then stopped out.",
            "Exits only after the market has already given back part of the move.",
            "Few trades on slow channels, so out-of-sample samples are small.",
        ),
    )

    def __init__(
        self,
        symbol: str,
        entry_lookback: int = DEFAULT_ENTRY_LOOKBACK,
        exit_lookback: int = DEFAULT_EXIT_LOOKBACK,
        confidence: float = DEFAULT_CONFIDENCE,
    ) -> None:
        for label, value in (("entry_lookback", entry_lookback), ("exit_lookback", exit_lookback)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{label} must be an integer, got {value!r}")
        if entry_lookback < 2:
            raise ValueError(f"entry_lookback must be at least 2, got {entry_lookback}")
        if exit_lookback < 1:
            raise ValueError(f"exit_lookback must be at least 1, got {exit_lookback}")
        if exit_lookback > entry_lookback:
            raise ValueError(
                f"exit_lookback ({exit_lookback}) must not exceed entry_lookback ({entry_lookback})"
            )
        super().__init__(name="donchian_breakout", symbol=symbol)
        self._entry = entry_lookback
        self._exit = exit_lookback
        self._confidence = confidence

    @property
    def params(self) -> dict:
        return {
            "entry_lookback": self._entry,
            "exit_lookback": self._exit,
            "confidence": self._confidence,
        }

    def history_requirement(self) -> HistoryRequirement:
        """The longest channel's prior bars plus the decision bar itself."""
        longest = max(self._entry, self._exit)
        return combine(
            component(
                f"Donchian({longest})",
                longest + 1,
                f"{longest} prior bars for the channel plus the decision bar",
            )
        )

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        self.require_columns(data, "high", "low", "close")
        out = data.copy()
        out["channel_high"] = self.indicator(data, "DONCHIAN_UPPER", period=self._entry)
        out["channel_low"] = self.indicator(data, "DONCHIAN_LOWER", period=self._exit)
        return out

    def generate_signals(self, data: pd.DataFrame) -> list[Signal]:
        signals: list[Signal] = []
        in_position = False
        for timestamp, row in data.iterrows():
            close = row["close"]
            high, low = row["channel_high"], row["channel_low"]
            if not in_position:
                if pd.isna(high):
                    continue  # channel not formed yet -- no opinion
                if close > high:
                    signals.append(
                        self.emit_signal(
                            timestamp,
                            SignalDirection.LONG,
                            confidence=self._confidence,
                            reason=f"close {close:.4f} broke above the prior {self._entry}-bar high {high:.4f}",
                        )
                    )
                    in_position = True
            else:
                if pd.isna(low):
                    continue
                if close < low:
                    signals.append(
                        self.emit_signal(
                            timestamp,
                            SignalDirection.FLAT,
                            confidence=self._confidence,
                            reason=f"close {close:.4f} broke below the prior {self._exit}-bar low {low:.4f}",
                        )
                    )
                    in_position = False
        return signals
