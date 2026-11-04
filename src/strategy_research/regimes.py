"""Performance by market regime, using the existing ``MarketRegimeEngine`` -- no new regime labels (ADR-0061).

The engine only knows two axes with real logic: trend (``trending`` / ``ranging``, direction-agnostic) and
volatility (``volatile`` / ``low_volatility``). There is no bullish/bearish label and none is invented here. A bar's
return is attributed to the regime known at the *previous* bar's close, so a regime label never uses the bar it
describes. Bars whose regime scores are still undefined (indicator warm-up) are reported as ``unknown``.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from src.regime.engine import MarketRegimeEngine


def regime_labels(candles: pd.DataFrame) -> pd.DataFrame:
    engine = MarketRegimeEngine(candles)
    scores = engine.score()
    labels = engine.dominant(scores)
    undefined = scores["trending"].isna() | scores["ranging"].isna()
    labels.loc[undefined, "trend_regime"] = "unknown"
    undefined_v = scores["volatile"].isna() | scores["low_volatility"].isna()
    labels.loc[undefined_v, "volatility_regime"] = "unknown"
    return labels[["trend_regime", "volatility_regime"]]


def regime_breakdown(candles: pd.DataFrame, net_equity: pd.Series, i0: int, i1: int) -> dict[str, list[dict[str, Any]]]:
    """Compounded net strategy return and bar count per regime label, over bars ``[i0, i1)``."""
    labels = regime_labels(candles).shift(1).fillna("unknown")
    returns = net_equity.pct_change().fillna(0.0)
    out: dict[str, list[dict[str, Any]]] = {}
    for axis in ("trend_regime", "volatility_regime"):
        rows = []
        window_labels = labels[axis].iloc[i0:i1]
        window_returns = returns.iloc[i0:i1]
        for label in sorted(window_labels.unique()):
            r = window_returns[window_labels == label]
            rows.append({"regime": label, "bars": int(len(r)), "net_return": float((1.0 + r).prod() - 1.0)})
        out[axis] = rows
    return out
