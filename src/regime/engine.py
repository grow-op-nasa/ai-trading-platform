"""The Market Regime Engine.

Scores every candle against six named regimes, organized as three
independent axes:

    trend axis:       trending      vs. ranging
    volatility axis:  volatile      vs. low_volatility
    risk axis:        risk_on       vs. risk_off

All six names are exposed from day one (`REGIME_NAMES`) even though
only the trend and volatility axes have real logic today. The risk
axis returns NaN for every candle until a market-wide risk proxy (e.g.
VIX, or credit spreads) exists as a data source -- there is nothing to
compute it from yet. Keeping all six columns present now, rather than
adding risk_on/risk_off later, means a strategy written against this
engine's output today doesn't need to change when the risk axis is
implemented; the columns already exist, they just start returning real
numbers instead of NaN.

Scores are continuous in [0, 1], not just binary labels, so a candle
can be e.g. 0.7 trending / 0.3 ranging rather than only ever fully one
or the other -- `dominant()` collapses each axis to a label when a
single classification is actually needed (e.g. for a report).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.indicators.engine import IndicatorEngine

REGIME_NAMES = [
    "trending",
    "ranging",
    "volatile",
    "low_volatility",
    "risk_on",
    "risk_off",
]


class MarketRegimeEngine:
    """Scores candles against the six named regimes.

    Args:
        candles: OHLCV DataFrame (the `MarketDataService` contract).
        engine: an `IndicatorEngine` to reuse. Constructed from
            `candles` if not given -- indicators are never recomputed
            from scratch here, they're asked of the Indicator Engine,
            same as any other consumer.
    """

    def __init__(self, candles: pd.DataFrame, engine: IndicatorEngine | None = None) -> None:
        self._candles = candles
        self._engine = engine or IndicatorEngine(candles)

    def score(
        self,
        trend_fast: int = 20,
        trend_slow: int = 50,
        trend_saturation: float = 0.02,
        volatility_period: int = 14,
        volatility_lookback: int = 100,
    ) -> pd.DataFrame:
        """Score every candle against all six regimes.

        Args:
            trend_fast, trend_slow: SMA periods used for the trend axis.
            trend_saturation: separation (as a fraction of price) at
                which the trend score saturates towards 1. Smaller
                values make the trend axis more sensitive.
            volatility_period: ATR period used for the volatility axis.
            volatility_lookback: window used to rank current volatility
                against recent history.

        Returns:
            A DataFrame aligned to `candles`' index with one column per
            name in `REGIME_NAMES`, each a score in [0, 1] -- or NaN for
            `risk_on`/`risk_off`, which aren't computable yet.
        """
        trend_score = self._trend_score(trend_fast, trend_slow, trend_saturation)
        volatility_score = self._volatility_score(volatility_period, volatility_lookback)
        risk_score = pd.Series(np.nan, index=self._candles.index)

        return pd.DataFrame(
            {
                "trending": trend_score,
                "ranging": 1 - trend_score,
                "volatile": volatility_score,
                "low_volatility": 1 - volatility_score,
                "risk_on": risk_score,
                "risk_off": risk_score,
            },
            index=self._candles.index,
        )

    def dominant(self, scores: pd.DataFrame | None = None, **score_kwargs) -> pd.DataFrame:
        """Collapse each axis to its winning label per candle.

        Args:
            scores: precomputed output of `score()`. Computed fresh
                (with `score_kwargs` forwarded) if not given.

        Returns:
            A DataFrame with columns `trend_regime`, `volatility_regime`,
            `risk_regime` -- string labels. `risk_regime` is `"unknown"`
            wherever the risk axis is NaN (i.e. everywhere, today).
        """
        if scores is None:
            scores = self.score(**score_kwargs)

        trend_regime = np.where(
            scores["trending"] >= scores["ranging"], "trending", "ranging"
        )
        volatility_regime = np.where(
            scores["volatile"] >= scores["low_volatility"], "volatile", "low_volatility"
        )
        risk_regime = np.where(
            scores["risk_on"].isna(),
            "unknown",
            np.where(scores["risk_on"] >= scores["risk_off"], "risk_on", "risk_off"),
        )

        return pd.DataFrame(
            {
                "trend_regime": trend_regime,
                "volatility_regime": volatility_regime,
                "risk_regime": risk_regime,
            },
            index=scores.index,
        )

    def _trend_score(self, fast: int, slow: int, saturation: float) -> pd.Series:
        sma_fast = self._engine.calculate("SMA", period=fast)
        sma_slow = self._engine.calculate("SMA", period=slow)

        separation = (sma_fast - sma_slow).abs() / sma_slow
        # Saturating curve: 0 at no separation, approaches 1 as
        # separation grows past `saturation`. Never hits exactly 1.
        score = separation / (separation + saturation)
        return score.rename("trend_score")

    def _volatility_score(self, period: int, lookback: int) -> pd.Series:
        atr = self._engine.calculate("ATR", period=period)
        atr_pct = atr / self._candles["close"]

        def percentile_rank(window: pd.Series) -> float:
            current = window.iloc[-1]
            if pd.isna(current):
                return np.nan
            valid = window.dropna()
            if len(valid) < 2:
                return np.nan
            return float((valid < current).mean())

        score = atr_pct.rolling(window=lookback, min_periods=period).apply(
            percentile_rank, raw=False
        )
        return score.rename("volatility_score")
