"""Data shapes produced by Performance Attribution.

Plain dataclasses, no behavior beyond simple derived rendering -- the
`PerformanceAttributor` (`engine.py`) does the actual computation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.utils.formatting import format_timedelta

#: Bucket key (and displayed label) for a trade whose entry fell inside
#: the regime engine's indicator warmup period -- there's no reliable
#: trend/volatility score yet at that point, so it's kept out of
#: "Best"/"Worst Regime" rather than silently defaulting to a
#: misleading label.
UNKNOWN_REGIME = "unknown"


@dataclass
class RegimeStats:
    """Trade outcomes for one regime bucket (e.g. "Trending + Low Volatility")."""

    label: str
    trade_count: int
    win_rate: float | None
    avg_return_pct: float | None


@dataclass
class AttributionReport:
    """Everything Performance Attribution produced for one backtest."""

    total_trades: int
    winning_trades: int
    win_rate: float | None
    average_hold: pd.Timedelta | None
    regime_breakdown: dict[str, RegimeStats] = field(default_factory=dict)
    best_regime: str | None = None
    worst_regime: str | None = None

    def report(self) -> str:
        """A short, human-readable summary -- inspect `regime_breakdown`
        directly for the full per-bucket numbers."""
        lines = [
            f"Trades: {self.total_trades}",
            f"Winning Trades: {self.winning_trades}",
        ]
        if self.win_rate is not None:
            lines.append(f"Win Rate: {self.win_rate:.1%}")
        if self.average_hold is not None:
            lines.append(f"Average Hold: {format_timedelta(self.average_hold)}")
        if self.best_regime is not None:
            lines.append(f"Best Regime: {self.best_regime}")
        if self.worst_regime is not None:
            lines.append(f"Worst Regime: {self.worst_regime}")
        return "\n".join(lines)
