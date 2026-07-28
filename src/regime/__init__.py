"""Market Regime Detection.

Pure rule-based classification of what kind of market conditions are
currently in effect -- not AI. Every strategy will be able to ask "what
regime are we in right now" via `MarketRegimeEngine`. Part of the
Sprint 2 research engine (`DECISIONS.md`, ADR-0009).

Public entry point: `MarketRegimeEngine`, in `engine.py`.
"""

from src.regime.engine import REGIME_NAMES, MarketRegimeEngine

__all__ = ["MarketRegimeEngine", "REGIME_NAMES"]
