"""EMA Cross + Volatility Filter -- the platform's third permanent
strategy, and the first ever promoted out of the Strategy Development
Agent (`src/ai/agents/strategy_dev/`, Sprint 14, `DECISIONS.md`
ADR-0047/ADR-0048).

**Provenance (Sprint 14 spec: production registration must carry full
lineage back to the candidate that produced it):**

    candidate_id:  241a3b657ba846169476e8aa374235a1
    source_hash:   c4f7c8c2cdc3f88bbe728560e2fd6549786694915592cbfd7a4e8e93da529f71
    spec_hash:     1715ff2619f763cd3ba9220016f8f017dab0ad5c233a305cb36795d6a926cae8
    agent_run_id:  3e8ee5af9206434e9b1f1b81f7c283c8  (anthropic/claude-sonnet-5)
    promoted_at:   2026-09-24T03:43:10.789233+00:00

The trading logic below is unchanged from what `strategy-promote`
recorded as `PROMOTED` -- registering this class here, under
`src.strategies.registry`, is the one deliberate, explicit, human-only
step ADR-0047 always said promotion would require and never perform
itself (`CandidateRegistry.promote()` only marks the candidate
registry; it has no path to `src.strategies.registry` at all). Writing
this file, reviewing the logic, and committing it is that step,
performed by a human after reading the candidate's full evidence via
`strategy-promote`'s printout -- not by the agent, and not
automatically by any tool in `src/ai/agents/strategy_dev`.

**What changed going from candidate to production strategy (cosmetic
only, logic untouched):** the class gained a docstring, type
annotations, and a `params` property (`ExperimentSpec.capture()`
reads this to reconstruct an equivalent instance by name, exactly as
`EMACrossStrategy`/`RSIMeanReversionStrategy` already do) -- the
`prepare()`/`generate_signals()` bodies are byte-for-byte the same
decision logic the candidate's development, validation, and final
out-of-sample backtests were evaluated against.

**The idea, as the candidate's own spec stated it:** short-period
EMA(8)/EMA(21) momentum crossover on QQQ, gated by an ATR(14)
volatility-percentage filter -- entries only fire when the fast EMA is
above the slow EMA *and* ATR/close is at or above a minimum threshold
(avoiding low-volatility chop); positions exit either on a bearish EMA
cross or when volatility falls back below the threshold, whichever
comes first. Long-only, single-position, LONG/FLAT signals only.

**Evidence at promotion time (small samples -- read the full
`strategy-promote` printout for the human reviewer's own caveats
before trusting this in anything beyond further research):**
development backtest (2018-2022): 27 trades, Sharpe 0.47, max
drawdown -2.81%. Validation backtest (2023 - 2024 H1): 7 trades,
Sharpe 1.70, max drawdown -1.16%. Final out-of-sample test (2024 H2 -
2025 H1, run exactly once, post-freeze): 5 trades, Sharpe 1.11, max
drawdown -0.95%. All three runs assumed zero fees/slippage and used
the platform's default risk/execution config -- registering this
strategy here makes it discoverable by name for further, more
rigorous experiments (larger samples, realistic costs, other
symbols/intervals, a head-to-head baseline comparison against
`ema_cross`), not a claim that those checks have already been done.
"""

from __future__ import annotations

import pandas as pd

from src.signals.models import Signal, SignalDirection
from src.strategies.registry import register_strategy
from src.strategies.sdk import BaseStrategy

DEFAULT_FAST = 8
DEFAULT_SLOW = 21
DEFAULT_ATR_PERIOD = 14
DEFAULT_MIN_ATR_PCT = 0.008
DEFAULT_CONFIDENCE = 0.6


@register_strategy("ema_cross_vol_filter")
class EMACrossVolFilterStrategy(BaseStrategy):
    """Long-only EMA(fast)/EMA(slow) momentum crossover, gated by an
    ATR-based volatility-percentage filter.

    Enters `LONG` the first time the fast EMA is above the slow EMA
    *and* `ATR(atr_period)/close` is at or above `min_atr_pct`, while
    flat. Exits to `FLAT` the first time, while long, either the trend
    reverses (fast EMA no longer above slow EMA) or volatility falls
    back below `min_atr_pct` -- whichever happens first; an exit is
    never blocked by the volatility filter, only an entry is. Emits a
    `Signal` only at those transitions -- sparse by construction
    (ADR-0015), not one signal per candle. Never emits `SHORT`.

    Args:
        symbol: which instrument this strategy instance decides for
            (`DECISIONS.md`, ADR-0033) -- attached to every `Signal` it
            emits via `BaseStrategy.emit_signal()`.
        fast: fast EMA period (`src.indicators`'s `"EMA"`).
        slow: slow EMA period.
        atr_period: ATR lookback period (`src.indicators`'s `"ATR"`).
        min_atr_pct: minimum `ATR/close` ratio required for a trend
            signal to be actionable -- below this, the strategy treats
            the market as too quiet for its momentum thesis to hold.
        confidence: fixed confidence attached to every signal this
            strategy emits, like `EMACrossStrategy`/
            `RSIMeanReversionStrategy` -- no natural continuous
            confidence measure for a threshold rule this simple, so a
            constant rather than a fabricated score.
    """

    def __init__(
        self,
        symbol: str,
        fast: int = DEFAULT_FAST,
        slow: int = DEFAULT_SLOW,
        atr_period: int = DEFAULT_ATR_PERIOD,
        min_atr_pct: float = DEFAULT_MIN_ATR_PCT,
        confidence: float = DEFAULT_CONFIDENCE,
    ) -> None:
        super().__init__(name="ema_cross_vol_filter", symbol=symbol)
        self._fast = fast
        self._slow = slow
        self._atr_period = atr_period
        self._min_atr_pct = min_atr_pct
        self._confidence = confidence

    @property
    def params(self) -> dict:
        """`fast`/`slow`/`atr_period`/`min_atr_pct`/`confidence` --
        everything `__init__` needs besides `symbol` to reconstruct an
        equivalent instance. Read by `ExperimentSpec.capture()`
        (`DECISIONS.md`, ADR-0035)."""
        return {
            "fast": self._fast,
            "slow": self._slow,
            "atr_period": self._atr_period,
            "min_atr_pct": self._min_atr_pct,
            "confidence": self._confidence,
        }

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        self.require_columns(data, "close")
        out = data.copy()
        out["ema_fast"] = self.indicator(data, "EMA", period=self._fast)
        out["ema_slow"] = self.indicator(data, "EMA", period=self._slow)
        out["atr"] = self.indicator(data, "ATR", period=self._atr_period)
        out["atr_pct"] = out["atr"] / out["close"]
        return out

    def generate_signals(self, data: pd.DataFrame) -> list[Signal]:
        signals: list[Signal] = []
        in_position = False

        for timestamp, row in data.iterrows():
            ema_fast = row["ema_fast"]
            ema_slow = row["ema_slow"]
            atr_pct = row["atr_pct"]

            if ema_fast != ema_fast or ema_slow != ema_slow or atr_pct != atr_pct:
                # NaN during indicator warmup period; skip until fully formed.
                continue

            trend_up = ema_fast > ema_slow
            vol_ok = atr_pct >= self._min_atr_pct

            if not in_position:
                if trend_up and vol_ok:
                    signals.append(
                        self.emit_signal(timestamp, SignalDirection.LONG, confidence=self._confidence)
                    )
                    in_position = True
            else:
                if (not trend_up) or (not vol_ok):
                    signals.append(
                        self.emit_signal(timestamp, SignalDirection.FLAT, confidence=self._confidence)
                    )
                    in_position = False

        return signals
