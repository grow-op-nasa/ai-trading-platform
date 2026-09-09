"""Position sizing and portfolio-level exposure limits.

Given a `Signal` and the account's current state, decides how large a
position to take -- and whether to take one at all, if doing so would
breach a configured exposure limit. Deliberately standalone from
`Backtester` and any broker/execution layer for now (`DECISIONS.md`,
ADR-0021).
"""

from src.risk.engine import PositionSizer
from src.risk.models import AccountState, RiskLimits, SizingDecision

__all__ = ["PositionSizer", "AccountState", "RiskLimits", "SizingDecision"]
