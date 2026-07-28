"""The Signal Framework.

The contract between strategies and everything downstream of them
(the Backtester, Performance Attribution, the Experiment Registry, the
AI Research Reporter). A `Signal` is a decision -- "what position
should the portfolio move toward, and how confident are we" -- not a
market event and not an order. See `DECISIONS.md`, ADR-0015.
"""

from src.signals.models import Signal, SignalDirection

__all__ = ["Signal", "SignalDirection"]
