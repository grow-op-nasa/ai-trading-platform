"""The `Signal` model -- one of the foundational contracts of the
platform (`DECISIONS.md`, ADR-0015).

A `Signal` answers exactly one question: "what position should the
portfolio move toward, and how confident are we?" It deliberately does
not answer "at what price" or "with which order type" -- that's not a
strategy's job. Strategies produce signals; the Backtester (and later,
`src/execution`) decides how to act on them.

    Signal(
        timestamp=candles.index[-1],
        direction=SignalDirection.LONG,
        confidence=0.87,
        metadata={"trend": "UP", "volatility": "LOW", "reason": "EMA20 crossed EMA50"},
    )

Signals are emitted sparsely -- one per decision point, not one per
candle. A strategy that's been LONG for the last 50 bars with nothing
new to say emits nothing on those bars; the Backtester holds the last
known direction until the next Signal arrives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

import pandas as pd


class SignalDirection(Enum):
    """The position a Signal says the portfolio should move toward."""

    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@dataclass(frozen=True)
class Signal:
    """One trading decision, with the evidence behind it.

    Immutable -- once emitted, a signal is a historical fact. `id` is
    assigned at construction time (a client-side UUID, not a database
    autoincrement) specifically so a Signal can be referenced -- e.g.
    by `Trade.entry_signal_id` in `src/backtesting` -- before it's ever
    persisted anywhere. The Experiment Registry is what actually stores
    Signals long-term (`DECISIONS.md`, ADR-0016); this class has no
    idea persistence exists.

    Args:
        timestamp: the candle at which this decision was made.
        direction: what position the portfolio should move toward.
        confidence: 0.0-1.0. How sure the strategy is, not a position
            size -- the current Backtester execution model is still
            single-unit regardless of confidence (see ADR-0011);
            sizing by confidence is `src/risk` territory, later.
        metadata: free-form evidence for downstream consumers --
            Performance Attribution and the AI Research Reporter read
            conventional keys like "trend", "volatility", "reason" out
            of this, but nothing here is schema-enforced.
        id: unique identifier for this signal. Auto-generated; only
            pass one explicitly when reconstructing a Signal that was
            previously read back from storage.

    Raises:
        ValueError: `confidence` is outside [0.0, 1.0].
    """

    timestamp: pd.Timestamp
    direction: SignalDirection
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"confidence must be between 0.0 and 1.0, got {self.confidence}"
            )
