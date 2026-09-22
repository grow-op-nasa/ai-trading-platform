"""Feature engineering -- src/ai/features.py.

Sprint 10 (`DECISIONS.md`, ADR-0043): the first stage of the platform's
ML research chain (`ROADMAP.md`, Sprint 10 -- "AI"). `FeatureBuilder`
turns canonical candles into a small, deliberately-not-clever table of
past-only numeric features -- it does not decide what a "good" feature
is beyond that, does not label anything, and has no idea a model or a
strategy exists downstream.

Every feature here is built from `src.indicators.IndicatorEngine` or a
plain pandas rolling/ewm/pct_change operation -- never a reimplemented
indicator formula (Sprint 10 spec, section 4: "do not duplicate
indicator formulas"). All of them are causal by construction: a rolling
window, an EWM, or `pct_change(n)` evaluated at row `t` only ever reads
rows `<= t`, so this module never needs to explicitly "shift" anything
to avoid lookahead -- the past-only property falls straight out of
using pandas' own causal operations and never a centered window, a
`shift(-n)`, or anything indexed by a future position (Sprint 10 spec,
section 5).

    from src.ai.features import FeatureBuilder, FeatureSpec

    builder = FeatureBuilder(FeatureSpec())
    features = builder.build(candles)   # DataFrame, NaN during warmup
    feature_id = builder.feature_set_id()

`FeatureSpec` is a plain, hashable configuration -- `feature_set_id()`
is its deterministic identity (Sprint 10 spec, section 35), so two
models trained on differently-parameterized feature sets never collide
under an ambiguous name like "EMA/RSI/ATR model".
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

import pandas as pd

from src.data.base import REQUIRED_COLUMNS
from src.indicators.engine import IndicatorEngine
from src.utils.hashing import sha256_hex

# The full, ordered list of columns FeatureBuilder.build() always
# produces, regardless of spec parameters -- column *names* are fixed,
# only their underlying periods vary with FeatureSpec. Fixed column
# order matters: it's part of what a trained model's schema check
# (Sprint 10 spec, section 60) compares against at inference time.
FEATURE_COLUMNS = (
    "return_1",
    "return_5",
    "return_20",
    "ema_diff",
    "rsi",
    "atr_norm",
    "macd_hist_norm",
    "volume_ratio",
)


@dataclass(frozen=True)
class FeatureSpec:
    """Deliberately small, explicit feature configuration (Sprint 10
    spec, section 4). Every field here is a parameter to an existing
    indicator or a plain pandas op -- adding a genuinely new feature
    means adding a column to `FEATURE_COLUMNS` and a case to
    `FeatureBuilder.build()`, not widening this dataclass with unrelated
    concerns.

    Args:
        return_periods: bar counts for the trailing-return features
            (`close[t] / close[t-n] - 1`). Three values, matching
            `FEATURE_COLUMNS`'s `return_1`/`return_5`/`return_20`.
        ema_fast / ema_slow: periods for the EMA-difference feature,
            normalized by `close` (`(EMA_fast - EMA_slow) / close`).
        rsi_period: period for `IndicatorEngine.calculate("RSI", ...)`.
        atr_period: period for `IndicatorEngine.calculate("ATR", ...)`,
            normalized by `close` (`ATR / close`) so it's comparable
            across instruments/price levels.
        macd_fast / macd_slow / macd_signal: periods for
            `IndicatorEngine.calculate("MACD", ...)`; the histogram
            column is normalized by `close`.
        volume_window: rolling window for the average volume the
            current bar's volume is compared against
            (`volume / rolling(volume_window).mean()`).

    Raises:
        ValueError: `return_periods` doesn't have exactly 3 entries, or
            any period/window value isn't a positive integer.
    """

    return_periods: tuple[int, int, int] = (1, 5, 20)
    ema_fast: int = 12
    ema_slow: int = 26
    rsi_period: int = 14
    atr_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    volume_window: int = 20

    def __post_init__(self) -> None:
        if len(self.return_periods) != 3:
            raise ValueError(
                f"return_periods must have exactly 3 entries "
                f"(matching return_1/return_5/return_20), got {self.return_periods!r}"
            )
        for name, value in {
            "return_periods[0]": self.return_periods[0],
            "return_periods[1]": self.return_periods[1],
            "return_periods[2]": self.return_periods[2],
            "ema_fast": self.ema_fast,
            "ema_slow": self.ema_slow,
            "rsi_period": self.rsi_period,
            "atr_period": self.atr_period,
            "macd_fast": self.macd_fast,
            "macd_slow": self.macd_slow,
            "macd_signal": self.macd_signal,
            "volume_window": self.volume_window,
        }.items():
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")

    def to_dict(self) -> dict:
        """Plain-dict rendering, for persistence (`src.ai.registry`) and
        for hashing (`feature_set_id()`)."""
        return dict(asdict(self), return_periods=list(self.return_periods))


class FeatureBuilder:
    """Builds `FEATURE_COLUMNS` from canonical OHLCV candles.

    Stateless beyond its own `spec` -- the same instance can be reused
    safely across multiple candle sets (matches
    `BaseStrategy.indicator()`'s own "built fresh each call" posture).

    Args:
        spec: feature configuration. Defaults to `FeatureSpec()`.
    """

    def __init__(self, spec: FeatureSpec | None = None) -> None:
        self._spec = spec or FeatureSpec()

    @property
    def spec(self) -> FeatureSpec:
        return self._spec

    def feature_set_id(self) -> str:
        """Deterministic hex digest of this builder's `spec` (Sprint 10
        spec, section 35) -- two `FeatureBuilder`s with equal `spec`
        values always agree, regardless of process or run."""
        return sha256_hex(
            json.dumps(self._spec.to_dict(), sort_keys=True).encode("utf-8")
        )

    def build(self, candles: pd.DataFrame) -> pd.DataFrame:
        """Compute `FEATURE_COLUMNS` for every row of `candles`.

        Returns a DataFrame aligned to `candles`' own index, `NaN`
        during each feature's own indicator warmup (Sprint 10 spec,
        section 9: warmup rows are surfaced here, not silently dropped
        or filled -- dropping them is `src.ai.dataset`'s job, once
        labels are known too). Never mutates `candles`.

        Raises:
            ValueError: `candles` is missing a required OHLCV column.
        """
        missing = [c for c in REQUIRED_COLUMNS if c not in candles.columns]
        if missing:
            raise ValueError(f"candles is missing required column(s): {missing}")

        spec = self._spec
        engine = IndicatorEngine(candles)
        close = candles["close"]

        r1, r5, r20 = spec.return_periods
        ema_fast = engine.calculate("EMA", period=spec.ema_fast)
        ema_slow = engine.calculate("EMA", period=spec.ema_slow)
        rsi = engine.calculate("RSI", period=spec.rsi_period)
        atr = engine.calculate("ATR", period=spec.atr_period)
        macd = engine.calculate(
            "MACD", fast=spec.macd_fast, slow=spec.macd_slow, signal=spec.macd_signal
        )
        volume_avg = candles["volume"].rolling(window=spec.volume_window).mean()

        features = pd.DataFrame(index=candles.index)
        features["return_1"] = close.pct_change(r1)
        features["return_5"] = close.pct_change(r5)
        features["return_20"] = close.pct_change(r20)
        features["ema_diff"] = (ema_fast - ema_slow) / close
        features["rsi"] = rsi
        features["atr_norm"] = atr / close
        features["macd_hist_norm"] = macd["histogram"] / close
        # volume_avg can be legitimately 0.0 (a synthetic/thin dataset) --
        # dividing by zero produces inf, not a silently wrong finite
        # number, so it's left to be caught as a NaN/inf row by
        # src.ai.dataset's dropna step rather than guarded into a
        # fabricated 0 here.
        features["volume_ratio"] = candles["volume"] / volume_avg

        return features[list(FEATURE_COLUMNS)]
