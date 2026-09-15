"""Position sizing and portfolio-level exposure limits.

Given a `Signal` and the account's current state, decides how large a
position to take -- and whether to take one at all, if doing so would
breach a configured exposure limit. Deliberately standalone from
`Backtester` and any broker/execution layer for now (`DECISIONS.md`,
ADR-0021). `AccountState` is a neutral domain model this package
consumes, not owns -- it lives in `src/portfolio` (ADR-0031) and is
re-exported here for convenience.
"""

from src.portfolio.models import AccountState
from src.risk.engine import PositionSizer
from src.risk.models import RiskLimits, SizingDecision

__all__ = ["PositionSizer", "AccountState", "RiskLimits", "SizingDecision"]
